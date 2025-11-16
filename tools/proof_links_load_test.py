"""Synthetic workload generator for ProofLinkHydrator + scoring pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Sequence

from app.config import settings
from app.models.company import CompanyProfile
from app.services.scoring.chatgpt_engine import ChatGPTScoringEngine, ScoringEngineError
from app.services.scoring.proof_links import ProofLinkError, ProofLinkHydrator

logger = logging.getLogger("tools.proof_links_load_test")

REPORT_VERSION = "2025-02-15"
DEFAULT_THRESHOLD_MS = 300.0
MAX_TASKS_WARNING = 50_000


@dataclass(frozen=True)
class LoadTestConfig:
    """Runtime configuration for the load harness."""

    companies_path: Path
    concurrency: int
    iterations: int
    warm_cache: bool
    report_path: Path | None
    force_report: bool
    scoring_run_id: str
    p95_threshold_ms: float


class LatencyRecorder:
    """Thread-safe collector for latency measurements."""

    def __init__(self, *, keyed: bool = False) -> None:
        self._lock = Lock()
        self._overall: list[float] = []
        self._per_key: dict[str, list[float]] | None = defaultdict(list) if keyed else None

    def record(self, duration_ms: float, *, key: str | None = None) -> None:
        with self._lock:
            self._overall.append(duration_ms)
            if self._per_key is not None and key:
                self._per_key[key].append(duration_ms)

    def snapshot(self) -> tuple[list[float], dict[str, list[float]]]:
        with self._lock:
            overall = list(self._overall)
            per_key = {slug: list(values) for slug, values in (self._per_key or {}).items()}
        return overall, per_key

    def reset(self) -> None:
        with self._lock:
            self._overall.clear()
            if self._per_key is not None:
                self._per_key.clear()


class InstrumentedProofLinkHydrator(ProofLinkHydrator):
    """ProofLinkHydrator that records per-slug latencies."""

    def __init__(self, *, recorder: LatencyRecorder, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._recorder = recorder

    def hydrate_many(  # type: ignore[override]
        self,
        company: CompanyProfile,
        slug: str,
        *,
        limit: int | None = None,
    ) -> list:
        start = time.perf_counter()
        try:
            return super().hydrate_many(company, slug, limit=limit)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._recorder.record(elapsed_ms, key=slug.lower())


class LoadTestRunner:
    """Orchestrates the concurrent workload and aggregates metrics."""

    def __init__(
        self,
        *,
        engine: ChatGPTScoringEngine,
        hydrator: InstrumentedProofLinkHydrator,
        companies: list[CompanyProfile],
        recorder: LatencyRecorder,
        config: LoadTestConfig,
        fixture_version: str,
    ) -> None:
        self._engine = engine
        self._hydrator = hydrator
        self._companies = companies
        self._recorder = recorder
        self._config = config
        self._fixture_version = fixture_version
        self._score_latencies = LatencyRecorder()
        self._errors: list[dict[str, Any]] = []
        self._error_lock = Lock()
        self._success_count = 0
        self._success_lock = Lock()

    def run(self) -> dict[str, Any]:
        if not self._companies:
            raise ValueError("Fixture file contained no companies.")
        if self._config.iterations <= 0 or self._config.concurrency <= 0:
            raise ValueError("Iterations and concurrency must be positive.")
        total_tasks = len(self._companies) * self._config.iterations
        if total_tasks > MAX_TASKS_WARNING:
            logger.warning(
                "Requested workload may exhaust memory: companies=%s iterations=%s total_tasks=%s",
                len(self._companies),
                self._config.iterations,
                total_tasks,
            )
        if self._config.warm_cache:
            logger.info("Warming hydrator cache with %s companies", len(self._companies))
            for company in self._companies:
                try:
                    self._engine.score_company(company, scoring_run_id=f"{self._config.scoring_run_id}-warmup", force=True)
                except Exception as exc:
                    logger.warning("Warmup error company_id=%s error=%s", company.company_id, exc)
            self._recorder.reset()
            self._score_latencies.reset()

        cache_baseline = self._hydrator.cache_stats
        start_ts = datetime.now(UTC)
        start = time.perf_counter()
        futures = []
        with ThreadPoolExecutor(max_workers=self._config.concurrency) as executor:
            for iteration in range(self._config.iterations):
                run_id = f"{self._config.scoring_run_id}-{iteration}"
                for company in self._companies:
                    futures.append(executor.submit(self._score_company, company, run_id, iteration))
            for future in as_completed(futures):
                future.result()
        elapsed = time.perf_counter() - start
        end_ts = datetime.now(UTC)
        cache_stats = self._hydrator.cache_stats
        delta_hits = max(0, cache_stats["hits"] - cache_baseline["hits"])
        delta_misses = max(0, cache_stats["misses"] - cache_baseline["misses"])
        hit_ratio = self._compute_ratio(delta_hits, delta_hits + delta_misses)

        hydrator_overall, per_slug = self._recorder.snapshot()
        scoring_overall, _ = self._score_latencies.snapshot()
        latency_summary = {
            "hydrator": {
                "overall": _summarize_latencies(hydrator_overall),
                "per_slug": {slug: _summarize_latencies(values) for slug, values in per_slug.items()},
            },
            "scoring": _summarize_latencies(scoring_overall),
        }
        errors = list(self._errors)
        error_counts = Counter(err["code"] for err in errors) if errors else Counter()
        throughput = self._compute_ratio(self._success_count, elapsed)
        metadata = {
            "report_version": REPORT_VERSION,
            "fixture_version": self._fixture_version,
            "companies_count": len(self._companies),
            "iterations": self._config.iterations,
            "concurrency": self._config.concurrency,
            "total_tasks": total_tasks,
            "start_time": start_ts.isoformat().replace("+00:00", "Z"),
            "end_time": end_ts.isoformat().replace("+00:00", "Z"),
            "elapsed_ms": round(elapsed * 1000, 2),
            "input_path": str(self._config.companies_path),
            "warm_cache": self._config.warm_cache,
        }
        return {
            "metadata": metadata,
            "latency_ms": latency_summary,
            "cache_stats": {
                "hits": delta_hits,
                "misses": delta_misses,
                "hit_ratio": hit_ratio,
            },
            "throughput_qps": throughput,
            "score_successes": self._success_count,
            "errors": errors,
            "error_summary": dict(error_counts),
        }

    def _score_company(self, company: CompanyProfile, scoring_run_id: str, iteration: int) -> None:
        start = time.perf_counter()
        try:
            self._engine.score_company(company, scoring_run_id=scoring_run_id, force=True)
        except ProofLinkError as exc:
            self._record_error(exc.code, str(exc), company, iteration)
        except ScoringEngineError as exc:
            code = getattr(exc, "code", "SCORING_ENGINE_ERROR")
            self._record_error(code, str(exc), company, iteration)
        except Exception as exc:  # pragma: no cover - defensive guard
            self._record_error("UNEXPECTED_ERROR", str(exc), company, iteration)
        else:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._score_latencies.record(elapsed_ms)
            with self._success_lock:
                self._success_count += 1

    def _record_error(self, code: str, message: str, company: CompanyProfile, iteration: int) -> None:
        error_payload = {
            "code": code,
            "message": message,
            "company_id": str(company.company_id),
            "iteration": iteration,
        }
        logger.error("proof_hydrator.load_test_error %s", json.dumps(error_payload, sort_keys=True))
        with self._error_lock:
            self._errors.append(error_payload)

    @staticmethod
    def _compute_ratio(numerator: float, denominator: float) -> float:
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator, 6)


def _summarize_latencies(values: Iterable[float]) -> dict[str, float]:
    samples = list(values)
    if not samples:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
    sorted_values = sorted(samples)
    return {
        "count": len(samples),
        "p50": _percentile(sorted_values, 0.50),
        "p95": _percentile(sorted_values, 0.95),
        "p99": _percentile(sorted_values, 0.99),
        "max": round(sorted_values[-1], 4),
        "avg": round(sum(sorted_values) / len(sorted_values), 4),
    }


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(sorted_values[0], 4)
    rank = quantile * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = rank - low
    value = sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction
    return round(value, 4)


def load_companies(path: Path) -> tuple[list[CompanyProfile], str]:
    """Load fixture companies from JSON."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    fixture_version = "unknown"
    raw_companies: list[dict[str, Any]]
    if isinstance(payload, dict):
        fixture_version = (
            payload.get("regression_version")
            or payload.get("fixture_version")
            or payload.get("version")
            or "unknown"
        )
        companies_field = payload.get("companies")
        if isinstance(companies_field, list):
            raw_companies = []
            for entry in companies_field:
                if isinstance(entry, dict) and "profile" in entry:
                    raw_companies.append(entry["profile"])
                elif isinstance(entry, dict):
                    raw_companies.append(entry)
            if not raw_companies:
                raise ValueError(f"No companies found in fixture {path}")
        else:
            raise ValueError(f"Unsupported fixture schema at {path}")
    elif isinstance(payload, list):
        raw_companies = payload
    else:
        raise ValueError(f"Unsupported fixture format at {path}")
    companies = [CompanyProfile(**company) for company in raw_companies]
    return companies, fixture_version


def run_load_test(
    config: LoadTestConfig,
    *,
    companies: list[CompanyProfile] | None = None,
    fixture_version: str | None = None,
) -> dict[str, Any]:
    """Entrypoint used by both CLI/tests and downstream benchmarks."""
    if companies is None:
        companies, resolved_fixture_version = load_companies(config.companies_path)
    else:
        resolved_fixture_version = fixture_version or "unknown"
    if not companies:
        raise ValueError("Fixture file contained no companies.")
    recorder = LatencyRecorder(keyed=True)
    hydrator = InstrumentedProofLinkHydrator(recorder=recorder, cache_ttl_seconds=settings.proof_cache_ttl_seconds)
    engine = ChatGPTScoringEngine(proof_hydrator=hydrator)
    runner = LoadTestRunner(
        engine=engine,
        hydrator=hydrator,
        companies=companies,
        recorder=recorder,
        config=config,
        fixture_version=resolved_fixture_version,
    )
    return runner.run()


def _read_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, value, default)
        return default


def _read_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, value, default)
        return default


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    default_concurrency = _read_int_env("PROOF_LOAD_CONCURRENCY", 8)
    default_iterations = _read_int_env("PROOF_LOAD_ITERATIONS", 50)
    parser = argparse.ArgumentParser(description="Stress-test ProofLinkHydrator with fixture companies.")
    parser.add_argument("--input", type=Path, required=True, help="Path to fixture companies JSON.")
    parser.add_argument("--concurrency", type=int, default=default_concurrency)
    parser.add_argument("--iterations", type=int, default=default_iterations)
    parser.add_argument("--no-warm-cache", action="store_true", help="Skip warmup pass before measuring.")
    parser.add_argument("--report", type=Path, default=_default_report_path())
    parser.add_argument("--force-report", action="store_true", help="Overwrite existing report file if set.")
    parser.add_argument("--scoring-run-id", type=str, default="proof-hydrator-load")
    parser.add_argument(
        "--p95-threshold-ms",
        type=float,
        default=_read_float_env("PROOF_P95_THRESHOLD_MS", DEFAULT_THRESHOLD_MS),
        help="Fail the harness when warmed-cache P95 latency exceeds this value.",
    )
    return parser.parse_args(argv)


def _default_report_path() -> Path | None:
    report_dir = os.getenv("PROOF_LOAD_REPORT_DIR")
    if not report_dir:
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(report_dir).expanduser() / f"proof_hydrator_report.{timestamp}.json"


def _ensure_report_path(path: Path | None, *, force: bool) -> None:
    if not path:
        return
    if path.exists() and not force:
        raise FileExistsError(f"Report already exists at {path}. Use --force-report to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)


def _emit_summary_logs(result: dict[str, Any]) -> None:
    hydrator_summary = result["latency_ms"]["hydrator"]["overall"]
    payload = {
        "event": "proof_hydrator.load_test",
        "p95_ms": hydrator_summary["p95"],
        "hit_ratio": result["cache_stats"]["hit_ratio"],
        "throughput_qps": result["throughput_qps"],
        "errors": result["error_summary"],
        "companies": result["metadata"]["companies_count"],
        "iterations": result["metadata"]["iterations"],
    }
    logger.info("proof_hydrator.load_test %s", json.dumps(payload, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        _ensure_report_path(args.report, force=args.force_report)
        config = LoadTestConfig(
            companies_path=args.input,
            concurrency=args.concurrency,
            iterations=args.iterations,
            warm_cache=not args.no_warm_cache,
            report_path=args.report,
            force_report=args.force_report,
            scoring_run_id=args.scoring_run_id,
            p95_threshold_ms=args.p95_threshold_ms,
        )
        result = run_load_test(config)
    except FileExistsError as exc:
        logger.error("CONFIGURATION_ERROR: %s", exc)
        return 3
    except FileNotFoundError as exc:
        logger.error("INPUT_NOT_FOUND: %s", exc)
        return 3
    except ValueError as exc:
        logger.error("CONFIGURATION_ERROR: %s", exc)
        return 3
    except Exception:  # pragma: no cover - defensive catch
        logger.exception("proof_links_load_test failed")
        return 3

    hydrator_p95 = result["latency_ms"]["hydrator"]["overall"]["p95"]
    if config.report_path:
        config.report_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    _emit_summary_logs(result)
    logger.info(
        "ProofLinkHydrator load test complete p95_ms=%.2f hit_ratio=%.2f successes=%s errors=%s",
        hydrator_p95,
        result["cache_stats"]["hit_ratio"],
        result["score_successes"],
        len(result["errors"]),
    )
    if hydrator_p95 > config.p95_threshold_ms:
        logger.error(
            "P95 latency %.2fms breached threshold %.2fms. "
            "Rerun with --p95-threshold-ms or investigate caching regressions.",
            hydrator_p95,
            config.p95_threshold_ms,
        )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
