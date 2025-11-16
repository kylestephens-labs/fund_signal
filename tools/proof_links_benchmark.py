"""Benchmark runner for ProofLinkHydrator cache behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from app.models.company import CompanyProfile
from app.config import settings
from tools import proof_links_load_test
from app.observability.metrics import metrics

logger = logging.getLogger("tools.proof_links_benchmark")

BENCHMARK_VERSION = "2025-02-20"
DEFAULT_FIXTURE = Path("tests/fixtures/scoring/proof_links_benchmark_companies.json")
DEFAULT_MIN_HIT_RATIO = 0.8


@dataclass(frozen=True)
class ProofLinksBenchmarkConfig:
    """Runtime settings shared by pytest + CLI entrypoints."""

    fixture_path: Path
    runs: int
    cold_runs: int
    concurrency: int
    sample_size: int | None
    skip_cold: bool
    report_path: Path | None
    force_report: bool
    scoring_run_id: str
    p95_threshold_ms: float


class BenchmarkFailure(RuntimeError):
    """Raised when benchmark configuration or thresholds are invalid."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def run_benchmark(config: ProofLinksBenchmarkConfig) -> dict[str, Any]:
    """Execute the cold + warm cache benchmark and return structured metrics."""
    if config.sample_size is not None and config.sample_size <= 0:
        raise BenchmarkFailure("Sample size must be positive.", code="BENCH_CONFIG_INVALID")
    companies, fixture_version, fixture_hash = _load_fixture(config.fixture_path)
    sampled = _select_companies(companies, config.sample_size)
    if not sampled:
        raise BenchmarkFailure("Fixture did not contain any companies.", code="BENCH_CONFIG_INVALID")
    if len(sampled) < 50:
        logger.warning("Benchmark running with %s companies (<50 may skew hit ratios).", len(sampled))
    if config.report_path:
        _ensure_report_path(config.report_path, force=config.force_report)
    cold_result: dict[str, Any] | None = None
    if not config.skip_cold:
        cold_result = _execute_phase(
            companies=sampled,
            fixture_version=fixture_version,
            iterations=config.cold_runs,
            warm_cache=False,
            concurrency=config.concurrency,
            scoring_run_id=f"{config.scoring_run_id}-cold",
            p95_threshold=config.p95_threshold_ms,
            source_path=config.fixture_path,
        )
    warm_result = _execute_phase(
        companies=sampled,
        fixture_version=fixture_version,
        iterations=config.runs,
        warm_cache=True,
        concurrency=config.concurrency,
        scoring_run_id=f"{config.scoring_run_id}-warm",
        p95_threshold=config.p95_threshold_ms,
        source_path=config.fixture_path,
    )
    metrics = _build_metrics(
        sampled_count=len(sampled),
        fixture_version=fixture_version,
        fixture_hash=fixture_hash,
        config=config,
        cold=cold_result,
        warm=warm_result,
    )
    _log_metrics(metrics)
    _emit_benchmark_metrics(metrics, threshold=config.p95_threshold_ms)
    if config.report_path:
        config.report_path.parent.mkdir(parents=True, exist_ok=True)
        config.report_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
        metrics["report_path"] = str(config.report_path)
    return metrics


def verify_thresholds(
    metrics: dict[str, Any],
    *,
    p95_threshold_ms: float,
    min_hit_ratio: float = DEFAULT_MIN_HIT_RATIO,
) -> None:
    """Ensure warmed-cache thresholds hold; raise when regressions appear."""
    warm = metrics.get("warm") or {}
    latency = warm.get("latency_ms") or {}
    warm_p95 = float(latency.get("p95", 0.0))
    hit_ratio = float(warm.get("cache_hit_ratio", 0.0))
    if warm_p95 > p95_threshold_ms:
        raise BenchmarkFailure(
            f"Warmed cache P95 {warm_p95:.2f}ms breached {p95_threshold_ms:.2f}ms.",
            code="BENCH_THRESHOLD_FAILED",
        )
    if hit_ratio < min_hit_ratio:
        raise BenchmarkFailure(
            f"Cache hit ratio {hit_ratio:.2f} below minimum {min_hit_ratio:.2f}.",
            code="BENCH_THRESHOLD_FAILED",
        )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Benchmark ProofLinkHydrator cache performance.")
    parser.add_argument("--input", type=Path, default=DEFAULT_FIXTURE, help="Path to benchmark fixture JSON.")
    parser.add_argument(
        "--runs",
        type=int,
        default=_read_int_env("BENCHMARK_RUNS", 200),
        help="Iterations for the warmed benchmark phase.",
    )
    parser.add_argument(
        "--cold-runs",
        type=int,
        default=_read_int_env("BENCHMARK_COLD_RUNS", 25),
        help="Iterations for the cold cache phase.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_read_int_env("PROOF_BENCH_CONCURRENCY", _read_int_env("PROOF_LOAD_CONCURRENCY", 8)),
        help="Worker threads used per phase.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=_read_optional_int_env("PROOF_BENCH_SAMPLE_SIZE"),
        help="Optional cap on sampled companies.",
    )
    parser.add_argument(
        "--skip-cold",
        action="store_true",
        default=_read_bool_env("PROOF_BENCH_SKIP_COLD", False),
        help="Skip the cold-cache warmup measurement.",
    )
    parser.add_argument(
        "--threshold-ms",
        type=float,
        default=_read_float_env("PROOF_BENCH_P95_THRESHOLD_MS", 300.0),
        help="Fail when warmed-cache P95 exceeds this limit.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional explicit report path (JSON).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=_default_report_dir(),
        help="Directory for timestamped reports when --report is omitted.",
    )
    parser.add_argument("--force-report", action="store_true", help="Overwrite an existing report file.")
    parser.add_argument(
        "--scoring-run-id",
        type=str,
        default="proof-links-benchmark",
        help="Prefix used for scoring run identifiers.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint used in docs."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv if argv is not None else sys.argv[1:])
    report_path = _resolve_report_path(args.report, args.report_dir)
    config = ProofLinksBenchmarkConfig(
        fixture_path=args.input,
        runs=args.runs,
        cold_runs=args.cold_runs,
        concurrency=args.concurrency,
        sample_size=args.sample_size,
        skip_cold=args.skip_cold,
        report_path=report_path,
        force_report=args.force_report,
        scoring_run_id=args.scoring_run_id,
        p95_threshold_ms=args.threshold_ms,
    )
    try:
        metrics = run_benchmark(config)
        verify_thresholds(metrics, p95_threshold_ms=config.p95_threshold_ms)
    except BenchmarkFailure as exc:
        logger.error("%s: %s", exc.code, exc)
        return 2 if exc.code == "BENCH_THRESHOLD_FAILED" else 3
    except FileNotFoundError as exc:
        logger.error("INPUT_NOT_FOUND: %s", exc)
        return 3
    except Exception:  # pragma: no cover - defensive catch
        logger.exception("proof_links_benchmark failed")
        return 3
    return 0


def _load_fixture(path: Path) -> tuple[list[CompanyProfile], str, str]:
    companies, fixture_version = proof_links_load_test.load_companies(path)
    fixture_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    return companies, fixture_version, fixture_hash


def _select_companies(companies: list[CompanyProfile], sample_size: int | None) -> list[CompanyProfile]:
    if sample_size is None or sample_size >= len(companies):
        return companies
    return companies[:sample_size]


def _execute_phase(
    *,
    companies: list[CompanyProfile],
    fixture_version: str,
    iterations: int,
    warm_cache: bool,
    concurrency: int,
    scoring_run_id: str,
    p95_threshold: float,
    source_path: Path,
) -> dict[str, Any]:
    config = proof_links_load_test.LoadTestConfig(
        companies_path=source_path,
        concurrency=concurrency,
        iterations=iterations,
        warm_cache=warm_cache,
        report_path=None,
        force_report=True,
        scoring_run_id=scoring_run_id,
        p95_threshold_ms=p95_threshold,
    )
    return proof_links_load_test.run_load_test(
        config,
        companies=companies,
        fixture_version=fixture_version,
    )


def _build_metrics(
    *,
    sampled_count: int,
    fixture_version: str,
    fixture_hash: str,
    config: ProofLinksBenchmarkConfig,
    cold: dict[str, Any] | None,
    warm: dict[str, Any],
) -> dict[str, Any]:
    cold_payload = _phase_payload(cold) if cold else None
    warm_payload = _phase_payload(warm)
    statsd_payload = {
        "proof_links.latency_p50": warm_payload["latency_ms"]["p50"],
        "proof_links.latency_p95": warm_payload["latency_ms"]["p95"],
        "proof_links.latency_p99": warm_payload["latency_ms"]["p99"],
        "proof_links.cache_hit_ratio": warm_payload["cache_hit_ratio"],
        "proof_links.throughput_qps": warm_payload["throughput_qps"],
    }
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "fixture_version": fixture_version,
        "fixture_hash": fixture_hash,
        "sample_size": sampled_count,
        "runs": config.runs,
        "cold_runs": config.cold_runs if not config.skip_cold else 0,
        "concurrency": config.concurrency,
        "scoring_run_id": config.scoring_run_id,
        "threshold_ms": config.p95_threshold_ms,
        "cold": cold_payload,
        "warm": warm_payload,
        "statsd_payload": statsd_payload,
    }


def _emit_benchmark_metrics(summary: dict[str, Any], *, threshold: float) -> None:
    warm = summary.get("warm")
    if not warm:
        return
    latency = warm["latency_ms"]
    tags = {
        "sample_size": str(summary.get("sample_size")),
        "runs": str(summary.get("runs")),
    }
    metrics.gauge("benchmark.latency_p50", latency["p50"], tags=tags)
    metrics.gauge("benchmark.latency_p95", latency["p95"], tags=tags)
    metrics.gauge("benchmark.latency_p99", latency["p99"], tags=tags)
    metrics.gauge("benchmark.cache_hit_ratio", warm["cache_hit_ratio"], tags=tags)
    metrics.gauge("benchmark.throughput_qps", warm["throughput_qps"], tags=tags)
    total_tasks = (summary.get("sample_size") or 0) * (summary.get("runs") or 0)
    error_count = sum(warm.get("errors", {}).values()) if warm.get("errors") else 0
    error_rate = 0.0 if not total_tasks else round(error_count / total_tasks, 4)
    metrics.gauge("benchmark.error_rate", error_rate, tags=tags)
    if latency["p95"] > threshold:
        metrics.alert(
            "benchmark.latency_p95",
            value=latency["p95"],
            threshold=threshold,
            severity="critical",
            tags=tags,
        )
    if error_rate > settings.render_alert_threshold_error:
        metrics.alert(
            "benchmark.error_rate",
            value=error_rate,
            threshold=settings.render_alert_threshold_error,
            severity="warning",
            tags=tags,
        )


def _phase_payload(result: dict[str, Any]) -> dict[str, Any]:
    hydrator = result["latency_ms"]["hydrator"]["overall"]
    metadata = result.get("metadata", {})
    return {
        "latency_ms": {
            "count": hydrator["count"],
            "p50": hydrator["p50"],
            "p95": hydrator["p95"],
            "p99": hydrator["p99"],
            "max": hydrator["max"],
            "avg": hydrator["avg"],
        },
        "cache_hits": result["cache_stats"]["hits"],
        "cache_misses": result["cache_stats"]["misses"],
        "cache_hit_ratio": result["cache_stats"]["hit_ratio"],
        "throughput_qps": result["throughput_qps"],
        "score_successes": result["score_successes"],
        "errors": result["error_summary"],
        "window": {
            "start": metadata.get("start_time"),
            "end": metadata.get("end_time"),
            "elapsed_ms": metadata.get("elapsed_ms"),
        },
    }


def _log_metrics(metrics: dict[str, Any]) -> None:
    payload = {
        "event": "proof_links.benchmark",
        "benchmark_version": metrics["benchmark_version"],
        "warm_p95_ms": metrics["warm"]["latency_ms"]["p95"],
        "warm_hit_ratio": metrics["warm"]["cache_hit_ratio"],
        "fixture_hash": metrics["fixture_hash"],
        "sample_size": metrics["sample_size"],
        "runs": metrics["runs"],
    }
    logger.info("proof_links.benchmark %s", json.dumps(payload, sort_keys=True))
    logger.info(
        "proof_links.latency_p95 %s",
        json.dumps(
            {
                "value_ms": metrics["warm"]["latency_ms"]["p95"],
                "threshold_ms": metrics["threshold_ms"],
                "report_version": metrics["benchmark_version"],
            },
            sort_keys=True,
        ),
    )


def _ensure_report_path(path: Path, *, force: bool) -> None:
    if not path:
        return
    if path.exists() and not force:
        raise BenchmarkFailure(f"Report already exists at {path}", code="BENCH_CONFIG_INVALID")


def _resolve_report_path(report: Path | None, report_dir: Path | None) -> Path | None:
    if report:
        return report
    if not report_dir:
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return report_dir / f"proof_links_benchmark.{timestamp}.json"


def _default_report_dir() -> Path | None:
    value = os.getenv("BENCHMARK_REPORT_DIR")
    return Path(value).expanduser() if value else None


def _read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw, default)
        return default


def _read_optional_int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r. Ignoring.", name, raw)
        return None


def _read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %.2f.", name, raw, default)
        return default


def _read_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid %s=%r. Falling back to %s.", name, raw, default)
    return default


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
