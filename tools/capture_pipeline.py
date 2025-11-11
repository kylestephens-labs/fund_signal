"""Capture pipeline CLI for Exa -> You.com -> Tavily data acquisition."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import hmac
import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from random import SystemRandom

from app.clients.tavily import TavilyClient, TavilyError, TavilyRateLimitError, TavilyTimeoutError
from app.clients.youcom import YoucomClient, YoucomError, YoucomRateLimitError, YoucomTimeoutError
from app.models.lead import CompanyFunding
from pipelines.day1 import tavily_confirm, youcom_verify
from pipelines.news_client import (
    FIXTURE_DIR_ENV,
    MODE_ENV,
    SOURCE_ENV,
    FixtureSource,
    RuntimeMode,
)
from pipelines.normalize import ensure_dir, slugify_company

logger = logging.getLogger("tools.capture_pipeline")

ProviderFn = Callable[[], list[dict]]
TOOL_VERSION = "1.0.0"
_RNG = SystemRandom()


class RateLimiter:
    """Simple token bucket enforcing QPS per provider."""

    def __init__(self, qps: float) -> None:
        if qps <= 0:
            raise ValueError("QPS must be positive.")
        self._min_interval = 1.0 / qps
        self._lock = threading.Lock()
        self._next_time = 0.0

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                wait_for = self._next_time - now
                if wait_for <= 0:
                    self._next_time = now + self._min_interval
                    return
                sleep_for = wait_for
            time.sleep(sleep_for)


@dataclass
class ProviderStats:
    """Track per-provider metrics."""

    name: str
    requests: int = 0
    successes: int = 0
    rate_limits: int = 0
    errors: int = 0
    total_samples: int = 0
    total_unique_domains: int = 0

    def record_request(self) -> None:
        self.requests += 1

    def record_success(self, raw_count: int, unique_domains: int) -> None:
        self.successes += 1
        self.total_samples += raw_count
        self.total_unique_domains += unique_domains

    def record_rate_limit(self) -> None:
        self.rate_limits += 1

    def record_error(self) -> None:
        self.errors += 1

    def to_dict(self) -> dict:
        ratio = (
            self.total_unique_domains / self.total_samples
            if self.total_samples
            else 0.0
        )
        return {
            "name": self.name,
            "requests": self.requests,
            "successes": self.successes,
            "rate_limits": self.rate_limits,
            "errors": self.errors,
            "dedup_ratio": round(ratio, 4),
        }


class JsonlCapture:
    """Manage JSONL files and processed slugs for resume support."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._processed = self._load_existing()
        ensure_dir(self._path.parent)

    def _load_existing(self) -> set[str]:
        processed: set[str] = set()
        if not self._path.exists():
            return processed
        with self._path.open("r", encoding="utf-8") as infile:
            for line in infile:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                slug = payload.get("slug")
                if isinstance(slug, str):
                    processed.add(slug)
        return processed

    def has(self, slug: str) -> bool:
        with self._lock:
            return slug in self._processed

    def append(self, slug: str, data: list[dict]) -> None:
        record = {"slug": slug, "data": data, "timestamp": datetime.now(tz=UTC).isoformat()}
        line = json.dumps(record, separators=(",", ":"))
        with self._lock, self._path.open("a", encoding="utf-8") as outfile:
            outfile.write(line + "\n")
        self._processed.add(slug)

    def read_all(self) -> list[dict]:
        results: list[dict] = []
        if not self._path.exists():
            return results
        with self._path.open("r", encoding="utf-8") as infile:
            for line in infile:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = payload.get("data")
                if isinstance(data, list):
                    results.extend(data)
        return results


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture Exa/You.com/Tavily data into offline fixtures.")
    parser.add_argument("--input", type=Path, required=True, help="Path to Exa seed JSON.")
    parser.add_argument("--out", type=Path, default=Path("artifacts"), help="Base directory for capture bundles.")
    parser.add_argument("--bundle", type=Path, help="Existing bundle directory (required for --resume).")
    parser.add_argument("--resume", action="store_true", help="Resume capturing within an existing bundle.")
    parser.add_argument("--concurrency", type=int, default=4, help="Maximum number of concurrent company captures.")
    parser.add_argument("--qps-youcom", type=float, default=2.0, help="You.com QPS limit.")
    parser.add_argument("--qps-tavily", type=float, default=2.0, help="Tavily QPS limit.")
    parser.add_argument("--max-attempts", type=int, default=5, help="Max attempts per provider call.")
    parser.add_argument("--expiry-days", type=int, default=7, help="Manifest expiry window in days.")
    return parser.parse_args(argv)


def build_bundle_dir(args: argparse.Namespace) -> Path:
    if args.resume:
        if not args.bundle or not args.bundle.exists():
            raise ValueError("--resume requires --bundle pointing to an existing capture directory.")
        return args.bundle
    timestamp = datetime.now(tz=UTC)
    date_prefix = timestamp.strftime("%Y/%m/%d")
    bundle_id = f"bundle-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
    bundle_dir = ensure_dir(args.out / date_prefix / bundle_id)
    return bundle_dir


def copy_input_seed(input_path: Path, raw_dir: Path) -> Path:
    ensure_dir(raw_dir)
    target = raw_dir / "exa_seed.json"
    data = json.loads(input_path.read_text(encoding="utf-8"))
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target


def canonical_domain(url: str) -> str:
    if "://" in url:
        host = url.split("://", 1)[1]
    else:
        host = url
    host = host.split("/", 1)[0].lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def count_unique_domains(records: Iterable[dict]) -> int:
    domains = {canonical_domain(record.get("url", "")) for record in records if record.get("url")}
    return len({domain for domain in domains if domain})


def call_with_retries(
    name: str,
    fn: ProviderFn,
    stats: ProviderStats,
    max_attempts: int,
    rate_limiter: RateLimiter,
) -> list[dict]:
    attempt = 0
    while True:
        attempt += 1
        rate_limiter.acquire()
        stats.record_request()
        try:
            return fn()
        except (YoucomRateLimitError, TavilyRateLimitError):
            stats.record_rate_limit()
            _sleep_with_backoff(attempt)
            if attempt >= max_attempts:
                raise
        except (YoucomTimeoutError, TavilyTimeoutError):
            stats.record_error()
            _sleep_with_backoff(attempt)
            if attempt >= max_attempts:
                raise
        except (YoucomError, TavilyError) as exc:
            stats.record_error()
            logger.error("%s capture failed permanently: %s", name, exc)
            raise


def _sleep_with_backoff(attempt: int) -> None:
    base = min(60, 2 ** attempt)
    jitter = _RNG.uniform(0, base * 0.25)
    time.sleep(base + jitter)


def capture_company(
    lead: CompanyFunding,
    youcom_capture: JsonlCapture,
    tavily_capture: JsonlCapture,
    youcom_client: YoucomClient,
    tavily_client: TavilyClient,
    youcom_stats: ProviderStats,
    tavily_stats: ProviderStats,
    youcom_limiter: RateLimiter,
    tavily_limiter: RateLimiter,
    max_attempts: int,
) -> None:
    slug = slugify_company(lead.company)
    query_youcom = youcom_verify.build_query(lead)
    query_tavily = tavily_confirm.build_query(lead)

    _maybe_capture_provider(
        slug=slug,
        company=lead.company,
        capture=youcom_capture,
        stats=youcom_stats,
        limiter=youcom_limiter,
        max_attempts=max_attempts,
        provider_name="You.com",
        fetch=lambda: youcom_client.search_news(query=query_youcom, limit=8),
    )

    _maybe_capture_provider(
        slug=slug,
        company=lead.company,
        capture=tavily_capture,
        stats=tavily_stats,
        limiter=tavily_limiter,
        max_attempts=max_attempts,
        provider_name="Tavily",
        fetch=lambda: tavily_client.search(query=query_tavily, max_results=8),
    )


def _maybe_capture_provider(
    *,
    slug: str,
    company: str,
    capture: JsonlCapture,
    stats: ProviderStats,
    limiter: RateLimiter,
    max_attempts: int,
    provider_name: str,
    fetch: ProviderFn,
) -> None:
    if capture.has(slug):
        return

    raw_results = call_with_retries(
        provider_name,
        fetch,
        stats,
        max_attempts,
        limiter,
    )
    unique_domains = count_unique_domains(raw_results)
    stats.record_success(len(raw_results), unique_domains)
    capture.append(slug, raw_results)
    logger.info("Captured %s records for %s (%s)", provider_name, company, len(raw_results))


def finalize_fixtures(bundle_dir: Path, youcom_capture: JsonlCapture, tavily_capture: JsonlCapture) -> Path:
    fixtures_dir = ensure_dir(bundle_dir / "fixtures")
    ensure_dir(fixtures_dir / "youcom")
    ensure_dir(fixtures_dir / "tavily")

    (fixtures_dir / "youcom" / "articles.json").write_text(
        json.dumps(youcom_capture.read_all(), indent=2),
        encoding="utf-8",
    )
    (fixtures_dir / "tavily" / "articles.json").write_text(
        json.dumps(tavily_capture.read_all(), indent=2),
        encoding="utf-8",
    )
    return fixtures_dir


def run_offline_pipelines(bundle_dir: Path, exa_seed_path: Path, fixtures_dir: Path) -> None:
    leads_dir = ensure_dir(bundle_dir / "leads")
    os.environ[MODE_ENV] = RuntimeMode.FIXTURE.value
    os.environ[SOURCE_ENV] = FixtureSource.LOCAL.value
    os.environ[FIXTURE_DIR_ENV] = str(fixtures_dir)

    youcom_output = leads_dir / "youcom_verified.json"
    tavily_output = leads_dir / "tavily_confirmed.json"

    youcom_verify.run_pipeline(
        input_path=exa_seed_path,
        output_path=youcom_output,
        min_articles=2,
        max_results=8,
    )

    tavily_confirm.run_pipeline(
        input_path=youcom_output,
        output_path=tavily_output,
        min_confirmations=2,
        max_results=8,
    )


def write_manifest(bundle_dir: Path, args: argparse.Namespace, youcom_stats: ProviderStats, tavily_stats: ProviderStats) -> None:
    manifest = {
        "schema_version": 1,
        "bundle_id": bundle_dir.name,
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "expiry_days": args.expiry_days,
        "git_commit": get_git_commit(),
        "tool_version": TOOL_VERSION,
        "providers": [
            youcom_stats.to_dict(),
            tavily_stats.to_dict(),
        ],
        "files": gather_file_metadata(bundle_dir),
    }
    manifest_no_sig = dict(manifest)
    signature = sign_manifest(manifest_no_sig)
    manifest["signature"] = signature
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def gather_file_metadata(bundle_dir: Path) -> list[dict]:
    """Return relative path, size, and checksum for bundle files."""
    entries: list[dict] = []
    for file_path in sorted(bundle_dir.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(bundle_dir).as_posix()
        if rel_path == "manifest.json":
            continue
        entries.append(
            {
                "path": rel_path,
                "size": file_path.stat().st_size,
                "checksum": sha256_file(file_path),
            }
        )
    return entries


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as infile:
        for chunk in iter(lambda: infile.read(1024 * 64), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S603, S607 - fixed command invoking git
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    return result.stdout.strip()


def sign_manifest(manifest: dict, key: str | None = None) -> str | None:
    secret = key or os.getenv("BUNDLE_HMAC_KEY")
    if not secret:
        return None
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return digest


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    bundle_dir = build_bundle_dir(args)
    raw_dir = ensure_dir(bundle_dir / "raw")
    youcom_capture = JsonlCapture(raw_dir / "youcom.jsonl")
    tavily_capture = JsonlCapture(raw_dir / "tavily.jsonl")

    leads = [CompanyFunding.model_validate(item) for item in json.loads(args.input.read_text(encoding="utf-8"))]
    exa_seed_path = copy_input_seed(args.input, raw_dir)

    youcom_stats = ProviderStats("youcom")
    tavily_stats = ProviderStats("tavily")
    youcom_limiter = RateLimiter(args.qps_youcom)
    tavily_limiter = RateLimiter(args.qps_tavily)

    try:
        with ExitStack() as stack:
            youcom_client = stack.enter_context(YoucomClient.from_env())
            tavily_client = stack.enter_context(TavilyClient.from_env())

            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                futures = [
                    executor.submit(
                        capture_company,
                        lead,
                        youcom_capture,
                        tavily_capture,
                        youcom_client,
                        tavily_client,
                        youcom_stats,
                        tavily_stats,
                        youcom_limiter,
                        tavily_limiter,
                        args.max_attempts,
                    )
                    for lead in leads
                ]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
    except ValueError as exc:
        logger.error("API client initialization failed: %s", exc)
        return 1

    fixtures_dir = finalize_fixtures(bundle_dir, youcom_capture, tavily_capture)
    run_offline_pipelines(bundle_dir, exa_seed_path, fixtures_dir)
    write_manifest(bundle_dir, args, youcom_stats, tavily_stats)
    logger.info("Capture complete. Bundle stored at %s", bundle_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
