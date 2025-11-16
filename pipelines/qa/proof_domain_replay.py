"""Replay stored scores to ensure proof domains remain secure."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from pydantic import ValidationError

from app.config import settings
from app.models.company import CompanyScore
from app.models.signal_breakdown import SignalProof
from app.services.scoring.proof_links import sanitize_proof_url
from scripts.backoff import exponential_backoff

logger = logging.getLogger("pipelines.qa.proof_domain_replay")

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=10.0)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
HEADERS = {
    "User-Agent": "FundSignal-ProofReplay/1.0",
    "Accept": "text/html,application/json;q=0.8,*/*;q=0.2",
}


class ProofDomainReplayError(RuntimeError):
    """Raised when the replay job cannot proceed."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReplayTarget:
    """Single proof entry lifted from a stored CompanyScore."""

    proof_hash: str
    source_url: str
    company_id: str
    company_name: str
    slug: str | None
    verified_by: list[str]
    scoring_run_id: str | None


@dataclass(frozen=True)
class ReplayCheckResult:
    """Outcome of resolving a proof URL."""

    final_url: str | None
    status_code: int | None
    redirect_count: int
    domain_changed: bool
    protocol_downgraded: bool
    success: bool
    error_code: str | None
    error_message: str | None
    final_domain: str | None


@dataclass(frozen=True)
class ReplayAuditRow:
    """Payload saved to Supabase for dashboards/alerts."""

    proof_hash: str
    company_id: str
    company_name: str
    slug: str | None
    initial_url: str
    final_url: str | None
    initial_domain: str
    final_domain: str | None
    protocol_downgraded: bool
    domain_changed: bool
    redirect_count: int
    status_code: int | None
    error_code: str | None
    error_message: str | None
    checked_at: datetime
    verified_by: list[str]
    scoring_run_id: str | None
    bundle_id: str | None
    replay_run_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proof_hash": self.proof_hash,
            "company_id": self.company_id,
            "company_name": self.company_name,
            "slug": self.slug,
            "initial_url": self.initial_url,
            "final_url": self.final_url,
            "initial_domain": self.initial_domain,
            "final_domain": self.final_domain,
            "protocol_downgraded": self.protocol_downgraded,
            "domain_changed": self.domain_changed,
            "redirect_count": self.redirect_count,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "verified_by": self.verified_by,
            "checked_at": self.checked_at.isoformat(),
            "scoring_run_id": self.scoring_run_id,
            "bundle_id": self.bundle_id,
            "replay_run_id": self.replay_run_id,
        }


@dataclass(frozen=True)
class ReplaySummary:
    total: int
    failures: int
    failure_rate: float
    duration_ms: float


class ReplayAuditStore(Protocol):
    async def upsert(self, rows: Sequence[ReplayAuditRow]) -> None: ...


class AlertPublisher(Protocol):
    async def publish(self, category: str, payload: Mapping[str, Any]) -> None: ...


class SupabaseReplayStore(ReplayAuditStore):
    """Writes replay rows to Supabase REST."""

    def __init__(self, *, base_url: str, service_key: str, table: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._table = table
        self._client = client
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }

    @property
    def _table_url(self) -> str:
        return f"{self._base}/rest/v1/{self._table}"

    async def upsert(self, rows: Sequence[ReplayAuditRow]) -> None:
        if not rows:
            return
        payload = [row.as_dict() for row in rows]
        response = await self._client.post(
            self._table_url,
            json=payload,
            params={"on_conflict": "proof_hash,checked_at"},
            headers=self._headers,
        )
        if response.status_code >= 400:
            raise ProofDomainReplayError(
                f"Supabase insert failed: {response.status_code}",
                code="E_SUPABASE_WRITE",
            )


class AlertDispatcher(AlertPublisher):
    def __init__(self, *, webhook_url: str | None, disabled: bool, client: httpx.AsyncClient) -> None:
        self._url = webhook_url
        self._disabled = disabled or not webhook_url
        self._client = client

    async def publish(self, category: str, payload: Mapping[str, Any]) -> None:
        if self._disabled:
            return
        response = await self._client.post(self._url, json={"category": category, "payload": payload})
        if response.status_code >= 400:
            logger.error(
                "proof_replay.alert_failed",
                extra={"category": category, "status": response.status_code},
            )


class ProofDomainReplay:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        audit_store: ReplayAuditStore,
        alert_publisher: AlertPublisher,
        concurrency: int,
        max_redirects: int,
        failure_threshold: float,
        bundle_id: str | None,
        replay_run_id: str | None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if max_redirects < 1:
            raise ValueError("max_redirects must be >= 1")
        self._client = http_client
        self._audit_store = audit_store
        self._alerts = alert_publisher
        self._concurrency = concurrency
        self._max_redirects = max_redirects
        self._failure_threshold = failure_threshold
        self._bundle_id = bundle_id
        self._replay_run_id = replay_run_id

    async def run(self, targets: Sequence[ReplayTarget]) -> ReplaySummary:
        if not targets:
            logger.info("proof_replay.no_targets")
            return ReplaySummary(total=0, failures=0, failure_rate=0.0, duration_ms=0.0)

        start = time.perf_counter()
        semaphore = asyncio.Semaphore(self._concurrency)
        tasks = [asyncio.create_task(self._resolve_target(target, semaphore)) for target in targets]
        rows: list[ReplayAuditRow] = []
        failures = 0
        insecure_events = 0
        for task in asyncio.as_completed(tasks):
            target, result = await task
            row = self._build_row(target, result)
            rows.append(row)
            if not result.success:
                failures += 1
            if result.protocol_downgraded or result.domain_changed:
                insecure_events += 1
                await self._alerts.publish(
                    "insecure_redirect",
                    {
                        "company": target.company_name,
                        "slug": target.slug,
                        "initial_url": target.source_url,
                        "final_url": result.final_url,
                        "reason": result.error_code,
                    },
                )
        await self._audit_store.upsert(rows)
        duration_ms = (time.perf_counter() - start) * 1000
        total = len(rows)
        failure_rate = (failures / total) if total else 0.0
        logger.info(
            "proof_replay.summary",
            extra={
                "proof_replay.checked_total": total,
                "proof_replay.failures_total": failures,
                "proof_replay.insecure_total": insecure_events,
                "duration_ms": f"{duration_ms:.2f}",
            },
        )
        if failure_rate > self._failure_threshold:
            await self._alerts.publish(
                "failure_rate",
                {
                    "failure_rate": f"{failure_rate:.2%}",
                    "threshold": f"{self._failure_threshold:.2%}",
                    "total": total,
                    "failures": failures,
                },
            )
            raise ProofDomainReplayError(
                f"Failure rate {failure_rate:.2%} exceeded threshold {self._failure_threshold:.2%}",
                code="E_REPLAY_FAILURE_RATE",
            )
        return ReplaySummary(total=total, failures=failures, failure_rate=failure_rate, duration_ms=duration_ms)

    async def _resolve_target(
        self,
        target: ReplayTarget,
        semaphore: asyncio.Semaphore,
    ) -> tuple[ReplayTarget, ReplayCheckResult]:
        async with semaphore:
            return target, await self._follow_redirects(target)

    async def _follow_redirects(self, target: ReplayTarget) -> ReplayCheckResult:
        url = target.source_url
        redirects = 0
        try:
            for attempt, delay in exponential_backoff(max_attempts=self._max_redirects, base_delay=0.2, factor=1.5, max_delay=2.0):
                response = await self._client.get(url, headers=HEADERS, follow_redirects=False)
                status = response.status_code
                if status in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if location:
                        next_url = urljoin(url, location)
                        sanitized = sanitize_proof_url(next_url)
                        desired_scheme = urlparse(next_url).scheme or "https"
                        parsed = urlparse(sanitized)
                        url = urlunparse(parsed._replace(scheme=desired_scheme))
                        redirects += 1
                        if redirects >= self._max_redirects:
                            break
                        await asyncio.sleep(delay)
                        continue
                final_url = str(response.request.url)
                return self._build_result(target.source_url, final_url, redirects, status, None, None)
        except httpx.HTTPStatusError as exc:
            final_url = str(exc.request.url)
            return self._build_result(target.source_url, final_url, redirects, exc.response.status_code, "status_error", str(exc))
        except httpx.RequestError as exc:
            url = str(exc.request.url) if exc.request else url
            return self._build_result(target.source_url, url, redirects, None, "526_TLS_FAILURE", str(exc))
        return self._build_result(target.source_url, url, redirects, None, "525_REDIRECT_LOOP", "Exceeded redirect policy")

    def _build_result(
        self,
        initial_url: str,
        final_url: str | None,
        redirects: int,
        status_code: int | None,
        error_code: str | None,
        error_message: str | None,
    ) -> ReplayCheckResult:
        canonical_initial = _canonical_domain(initial_url)
        canonical_final = _canonical_domain(final_url) if final_url else None
        protocol_downgraded = False
        domain_changed = False
        success = False
        if final_url:
            initial_scheme = urlparse(initial_url).scheme
            final_scheme = urlparse(final_url).scheme
            protocol_downgraded = initial_scheme == "https" and final_scheme == "http"
            domain_changed = canonical_final is not None and canonical_initial != canonical_final
            success = (
                not protocol_downgraded
                and not domain_changed
                and (status_code is None or status_code < 400)
                and error_code is None
            )
        if domain_changed and not error_code:
            error_code = "523_DOMAIN_CHANGED"
        if protocol_downgraded and not error_code:
            error_code = "524_PROTOCOL_DOWNGRADE"
        return ReplayCheckResult(
            final_url=final_url,
            status_code=status_code,
            redirect_count=redirects,
            domain_changed=domain_changed,
            protocol_downgraded=protocol_downgraded,
            success=success,
            error_code=error_code,
            error_message=error_message,
            final_domain=canonical_final,
        )

    def _build_row(self, target: ReplayTarget, result: ReplayCheckResult) -> ReplayAuditRow:
        now = datetime.now(UTC)
        return ReplayAuditRow(
            proof_hash=target.proof_hash,
            company_id=target.company_id,
            company_name=target.company_name,
            slug=target.slug,
            initial_url=target.source_url,
            final_url=result.final_url,
            initial_domain=_canonical_domain(target.source_url),
            final_domain=result.final_domain,
            protocol_downgraded=result.protocol_downgraded,
            domain_changed=result.domain_changed,
            redirect_count=result.redirect_count,
            status_code=result.status_code,
            error_code=result.error_code,
            error_message=result.error_message,
            checked_at=now,
            verified_by=target.verified_by,
            scoring_run_id=target.scoring_run_id,
            bundle_id=self._bundle_id,
            replay_run_id=self._replay_run_id,
        )


def load_scores(path: Path) -> list[ReplayTarget]:
    if not path.exists():
        raise ProofDomainReplayError(f"Score file not found: {path}", code="404_SCORES_MISSING")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProofDomainReplayError(f"Invalid JSON: {exc}", code="422_INVALID_SCORES") from exc

    if isinstance(payload, Mapping) and "scores" in payload:
        records = payload.get("scores", [])
    elif isinstance(payload, Sequence):
        records = payload
    else:
        raise ProofDomainReplayError("Unsupported score payload.", code="422_INVALID_SCORES")

    targets: list[ReplayTarget] = []
    seen: set[tuple[str, str]] = set()
    for entry in records:
        if not isinstance(entry, Mapping):
            raise ProofDomainReplayError("Score record must be an object.", code="422_INVALID_SCORES")
        try:
            score = CompanyScore(**entry)
        except ValidationError as exc:
            raise ProofDomainReplayError(f"Invalid score payload: {exc}", code="422_INVALID_SCORES") from exc
        company_name = (
            entry.get("company_name")
            or entry.get("company")
            or entry.get("name")
            or ""
        )
        for item in score.breakdown:
            slug = item.reason.lower()
            for proof in item.proofs:
                resolved = _build_replay_target(score, proof, slug, company_name)
                dedupe_key = (resolved.company_id, resolved.proof_hash)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                targets.append(resolved)
    return targets


def _build_replay_target(
    score: CompanyScore,
    proof: SignalProof,
    slug: str | None,
    company_name: str,
) -> ReplayTarget:
    sanitized = sanitize_proof_url(str(proof.source_url))
    return ReplayTarget(
        proof_hash=proof.proof_hash or sanitized,
        source_url=sanitized,
        company_id=str(score.company_id),
        company_name=company_name or str(score.company_id),
        slug=slug,
        verified_by=proof.verified_by,
        scoring_run_id=score.scoring_run_id,
    )


def _canonical_domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay stored scores and validate proof domains.")
    parser.add_argument("--scores", type=Path, required=True, help="Path to cached CompanyScore JSON.")
    parser.add_argument("--bundle-id", help="Bundle/run identifier for bookkeeping.")
    parser.add_argument("--replay-run-id", help="Unique replay identifier for auditing.")
    parser.add_argument("--supabase-table", default=settings.supabase_proof_replay_table, help="Supabase table for replay rows.")
    parser.add_argument("--concurrency", type=int, default=settings.proof_replay_concurrency, help="Concurrent HTTP checks.")
    parser.add_argument("--max-redirects", type=int, default=settings.proof_replay_max_redirects, help="Maximum redirects per proof.")
    parser.add_argument(
        "--failure-threshold",
        type=float,
        default=settings.proof_replay_failure_threshold,
        help="Alert threshold for failure rate.",
    )
    parser.add_argument("--alert-webhook", default=settings.proof_replay_alert_webhook, help="Webhook for replay alerts.")
    return parser.parse_args(argv)


async def _run_async(args: argparse.Namespace) -> ReplaySummary:
    scores = load_scores(args.scores)
    supabase_url = settings.supabase_url
    supabase_key = settings.supabase_service_key
    if not supabase_url or not supabase_key or not args.supabase_table:
        raise ProofDomainReplayError(
            "Supabase configuration missing (SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_PROOF_REPLAY_TABLE).",
            code="E_SUPABASE_CONFIG",
        )
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http_client, httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as supabase_client, httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as alert_client:
        audit_store = SupabaseReplayStore(
            base_url=supabase_url,
            service_key=supabase_key,
            table=args.supabase_table,
            client=supabase_client,
        )
        alerts = AlertDispatcher(
            webhook_url=args.alert_webhook,
            disabled=settings.proof_replay_disable_alerts,
            client=alert_client,
        )
        job = ProofDomainReplay(
            http_client=http_client,
            audit_store=audit_store,
            alert_publisher=alerts,
            concurrency=args.concurrency,
            max_redirects=args.max_redirects,
            failure_threshold=args.failure_threshold,
            bundle_id=args.bundle_id,
            replay_run_id=args.replay_run_id,
        )
        return await job.run(scores)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    summary = asyncio.run(_run_async(args))
    logger.info(
        "proof_replay.completed",
        extra={
            "checks": summary.total,
            "failures": summary.failures,
            "failure_rate": f"{summary.failure_rate:.2%}",
        },
    )


if __name__ == "__main__":
    try:
        main()
    except ProofDomainReplayError as exc:
        logger.error("proof_replay.failed", extra={"code": exc.code, "message": str(exc)})
        raise SystemExit(1) from exc
