"""Asynchronous job that validates proof links for evidence QA."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import httpx
from pydantic import ValidationError

from app.config import settings
from app.models.signal_breakdown import SignalProof
from app.services.scoring.proof_links import ProofLinkError, sanitize_proof_url
from scripts.backoff import exponential_backoff

logger = logging.getLogger("pipelines.qa.proof_link_monitor")

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0, read=10.0, write=10.0)
GET_FALLBACK_STATUSES = {405, 501}
REPEAT_ALERT_WINDOW = timedelta(hours=24)
HEADERS = {
    "User-Agent": "FundSignal-ProofQA/1.0",
    "Accept": "text/html,application/json;q=0.8,*/*;q=0.2",
}


class ProofLinkMonitorError(ProofLinkError):
    """Domain error exposed by the QA monitor."""


@dataclass(frozen=True)
class ProofCheckTarget:
    """Single proof link to validate."""

    proof_hash: str
    source_url: str
    company_id: str | None
    company_name: str | None
    slug: str | None
    bundle_id: str | None
    verified_by: list[str]
    timestamp: datetime | None


@dataclass(frozen=True)
class ProofCheckResult:
    """Network outcome for a single HEAD/GET attempt."""

    url: str
    status_code: int | None
    success: bool
    latency_ms: float | None
    attempts: int
    error_message: str | None
    error_code: str | None


@dataclass(frozen=True)
class ProofAuditRow:
    """Payload persisted to Supabase audits."""

    proof_hash: str
    source_url: str
    company_id: str | None
    company_name: str | None
    slug: str | None
    bundle_id: str | None
    http_status: int | None
    latency_ms: float | None
    retry_count: int
    last_checked_at: datetime
    last_success_at: datetime | None
    error_message: str | None
    error_code: str | None
    verified_by: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "proof_hash": self.proof_hash,
            "source_url": self.source_url,
            "company_id": self.company_id,
            "company_name": self.company_name,
            "slug": self.slug,
            "bundle_id": self.bundle_id,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "retry_count": self.retry_count,
            "last_checked_at": self.last_checked_at.isoformat(),
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "verified_by": self.verified_by,
        }


@dataclass(frozen=True)
class ProofAuditState:
    """Previous Supabase audit row for comparative alerting."""

    proof_hash: str
    http_status: int | None
    last_checked_at: datetime | None
    last_success_at: datetime | None


@dataclass(frozen=True)
class ProofMonitorSummary:
    """Aggregated metrics for an execution."""

    total: int
    failures: int
    failure_rate: float
    duration_ms: float


class ProofAuditStore(Protocol):
    """Persistence contract for QA runs."""

    async def upsert(self, rows: Sequence[ProofAuditRow]) -> None:
        ...

    async def fetch_latest(self, proof_hashes: set[str]) -> dict[str, ProofAuditState]:
        ...


class AlertPublisher(Protocol):
    """Notification sink for alerts."""

    async def publish(self, category: str, payload: Mapping[str, Any]) -> None:
        ...


class ProofLinkMonitor:
    """Coordinates asynchronous HEAD checks, persistence, and alerting."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        audit_store: ProofAuditStore,
        alert_publisher: AlertPublisher,
        concurrency: int,
        retry_limit: int,
        failure_threshold: float,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if retry_limit < 1:
            raise ValueError("retry_limit must be >= 1")
        self._client = http_client
        self._audit_store = audit_store
        self._alert_publisher = alert_publisher
        self._concurrency = concurrency
        self._retry_limit = retry_limit
        self._failure_threshold = failure_threshold

    async def run(self, targets: Sequence[ProofCheckTarget]) -> ProofMonitorSummary:
        if not targets:
            logger.info("proof_qa.no_targets")
            return ProofMonitorSummary(total=0, failures=0, failure_rate=0.0, duration_ms=0.0)

        start = time.perf_counter()
        grouped = self._group_targets(targets)
        previous = await self._audit_store.fetch_latest({target.proof_hash for target in targets})
        url_results = await self._check_urls(grouped.keys())

        now = datetime.now(UTC)
        rows: list[ProofAuditRow] = []
        repeated_failures: list[ProofAuditRow] = []
        failure_count = 0

        for url, result in url_results.items():
            for target in grouped[url]:
                prev = previous.get(target.proof_hash)
                row = self._build_audit_row(target, result, now, prev)
                rows.append(row)
                if not result.success:
                    failure_count += 1
                    if self._is_repeat_failure(prev, now):
                        repeated_failures.append(row)
                    logger.error(
                        "proof_qa.check_failed",
                        extra={
                            "company_id": target.company_id,
                            "slug": target.slug,
                            "status": result.status_code,
                            "error_code": result.error_code,
                            "url": target.source_url,
                        },
                    )

        await self._audit_store.upsert(rows)
        duration_ms = (time.perf_counter() - start) * 1000
        total = len(rows)
        failure_rate = (failure_count / total) if total else 0.0
        summary = ProofMonitorSummary(
            total=total,
            failures=failure_count,
            failure_rate=failure_rate,
            duration_ms=duration_ms,
        )

        if repeated_failures:
            await self._alert_publisher.publish(
                "repeat_failure",
                {
                    "count": len(repeated_failures),
                    "examples": [
                        {
                            "company": row.company_name,
                            "slug": row.slug,
                            "status": row.http_status,
                            "url": row.source_url,
                        }
                        for row in repeated_failures[:5]
                    ],
                },
            )

        logger.info(
            "proof_qa.run_complete",
            extra={
                "proof_qa.checks_total": summary.total,
                "proof_qa.failures_total": summary.failures,
                "proof_qa.failure_rate": f"{summary.failure_rate:.4f}",
                "duration_ms": f"{summary.duration_ms:.2f}",
            },
        )

        if summary.failure_rate > self._failure_threshold:
            await self._alert_publisher.publish(
                "failure_rate",
                {
                    "failure_rate": f"{summary.failure_rate:.2%}",
                    "threshold": f"{self._failure_threshold:.2%}",
                    "total": summary.total,
                    "failures": summary.failures,
                },
            )
            raise ProofLinkMonitorError(
                f"Failure rate {summary.failure_rate:.2%} exceeded threshold {self._failure_threshold:.2%}",
                code="598_TOO_MANY_FAILURES",
            )

        return summary

    def _group_targets(self, targets: Sequence[ProofCheckTarget]) -> dict[str, list[ProofCheckTarget]]:
        grouped: dict[str, list[ProofCheckTarget]] = defaultdict(list)
        for target in targets:
            grouped[target.source_url].append(target)
        return grouped

    async def _check_urls(self, urls: Iterable[str]) -> dict[str, ProofCheckResult]:
        ordered_urls = list(urls)
        if not ordered_urls:
            return {}
        semaphore = asyncio.Semaphore(self._concurrency)
        coroutines = [self._with_semaphore(semaphore, url) for url in ordered_urls]
        responses = await asyncio.gather(*coroutines, return_exceptions=True)

        results: dict[str, ProofCheckResult] = {}
        for url, outcome in zip(ordered_urls, responses, strict=False):
            if isinstance(outcome, ProofCheckResult):
                results[url] = outcome
                continue
            logger.exception("proof_qa.unhandled_exception", extra={"url": url})
            results[url] = ProofCheckResult(
                url=url,
                status_code=None,
                success=False,
                latency_ms=None,
                attempts=self._retry_limit,
                error_message=str(outcome),
                error_code="520_PROOF_QA_ERROR",
            )
        return results

    async def _with_semaphore(self, semaphore: asyncio.Semaphore, url: str) -> ProofCheckResult:
        async with semaphore:
            return await self._issue_request(url)

    async def _issue_request(self, url: str) -> ProofCheckResult:
        for attempt, delay in exponential_backoff(
            max_attempts=self._retry_limit,
            base_delay=0.5,
            factor=2.0,
            max_delay=5.0,
            jitter=0.2,
        ):
            start = time.perf_counter()
            try:
                response = await self._client.head(url, headers=HEADERS, follow_redirects=True)
                latency_ms = (time.perf_counter() - start) * 1000
                status = response.status_code
                if status in GET_FALLBACK_STATUSES:
                    response = await self._client.get(url, headers=HEADERS, follow_redirects=True)
                    latency_ms = (time.perf_counter() - start) * 1000
                    status = response.status_code
                if status < 400:
                    return ProofCheckResult(
                        url=url,
                        status_code=status,
                        success=True,
                        latency_ms=round(latency_ms, 2),
                        attempts=attempt,
                        error_message=None,
                        error_code=None,
                    )
                error_message = f"HTTP {status}"
                logger.warning(
                    "proof_qa.http_error",
                    extra={"url": url, "status": status, "attempt": attempt},
                )
                if attempt >= self._retry_limit:
                    return ProofCheckResult(
                        url=url,
                        status_code=status,
                        success=False,
                        latency_ms=round(latency_ms, 2),
                        attempts=attempt,
                        error_message=error_message,
                        error_code=f"{status}_HTTP_STATUS",
                    )
            except httpx.TimeoutException:
                error_message = "Timed out"
                error_code = "504_HEAD_TIMEOUT"
            except httpx.RequestError as exc:
                error_message = str(exc)
                cause = exc.__cause__
                if isinstance(cause, ssl.SSLError):
                    error_code = "523_TLS_HANDSHAKE_FAILED"
                else:
                    error_code = "520_PROOF_QA_ERROR"
                logger.warning(
                    "proof_qa.request_error",
                    extra={"url": url, "attempt": attempt, "code": error_code},
                )
            else:
                await asyncio.sleep(delay)
                continue

            if attempt >= self._retry_limit:
                return ProofCheckResult(
                    url=url,
                    status_code=None,
                    success=False,
                    latency_ms=None,
                    attempts=attempt,
                    error_message=error_message,
                    error_code=error_code,
                )
            await asyncio.sleep(delay)

        return ProofCheckResult(
            url=url,
            status_code=None,
            success=False,
            latency_ms=None,
            attempts=self._retry_limit,
            error_message="Exceeded retry policy",
            error_code="520_PROOF_QA_ERROR",
        )

    def _build_audit_row(
        self,
        target: ProofCheckTarget,
        result: ProofCheckResult,
        now: datetime,
        previous: ProofAuditState | None,
    ) -> ProofAuditRow:
        last_success_at = now if result.success else (previous.last_success_at if previous else None)
        return ProofAuditRow(
            proof_hash=target.proof_hash,
            source_url=target.source_url,
            company_id=target.company_id,
            company_name=target.company_name,
            slug=target.slug,
            bundle_id=target.bundle_id,
            http_status=result.status_code,
            latency_ms=result.latency_ms,
            retry_count=max(0, result.attempts - 1),
            last_checked_at=now,
            last_success_at=last_success_at,
            error_message=result.error_message,
            error_code=result.error_code,
            verified_by=target.verified_by,
        )

    def _is_repeat_failure(self, previous: ProofAuditState | None, now: datetime) -> bool:
        if not previous or previous.http_status is None:
            return False
        if previous.http_status < 400:
            return False
        if not previous.last_checked_at:
            return False
        return (now - previous.last_checked_at) <= REPEAT_ALERT_WINDOW


class SupabaseAuditClient(ProofAuditStore):
    """Persists audit rows via Supabase REST."""

    def __init__(
        self,
        *,
        base_url: str,
        service_key: str,
        table: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._table = table
        self._client = http_client
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }

    @property
    def _table_url(self) -> str:
        return f"{self._base}/rest/v1/{self._table}"

    async def upsert(self, rows: Sequence[ProofAuditRow]) -> None:
        if not rows:
            return
        payload = [row.as_dict() for row in rows]
        response = await self._client.post(
            self._table_url,
            json=payload,
            params={"on_conflict": "proof_hash,last_checked_at"},
            headers=self._headers,
        )
        if response.status_code >= 400:
            raise ProofLinkMonitorError(
                f"Supabase insert failed with status {response.status_code}",
                code="E_SUPABASE_WRITE",
            )

    async def fetch_latest(self, proof_hashes: set[str]) -> dict[str, ProofAuditState]:
        if not proof_hashes:
            return {}
        quoted = ",".join(f'"{value}"' for value in proof_hashes)
        params = {
            "select": "proof_hash,http_status,last_checked_at,last_success_at",
            "proof_hash": f"in.({quoted})",
            "order": "last_checked_at.desc",
        }
        response = await self._client.get(self._table_url, headers=self._headers, params=params)
        if response.status_code >= 400:
            raise ProofLinkMonitorError(
                f"Supabase query failed with status {response.status_code}",
                code="E_SUPABASE_FETCH",
            )
        records = response.json()
        latest: dict[str, ProofAuditState] = {}
        for record in records:
            proof_hash = record.get("proof_hash")
            if not proof_hash or proof_hash in latest:
                continue
            latest[proof_hash] = ProofAuditState(
                proof_hash=proof_hash,
                http_status=record.get("http_status"),
                last_checked_at=_parse_timestamp(record.get("last_checked_at")),
                last_success_at=_parse_timestamp(record.get("last_success_at")),
            )
        return latest


class AlertDispatcher(AlertPublisher):
    """Sends webhook notifications while supporting a kill switch."""

    def __init__(
        self,
        *,
        webhook_url: str | None,
        disabled: bool,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._url = webhook_url
        self._disabled = disabled or not webhook_url
        self._client = http_client

    async def publish(self, category: str, payload: Mapping[str, Any]) -> None:
        if self._disabled:
            return
        body = {
            "category": category,
            "payload": payload,
        }
        response = await self._client.post(self._url, json=body)
        if response.status_code >= 400:
            logger.error("proof_qa.alert_failed", extra={"category": category, "status": response.status_code})


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate SignalProof links asynchronously.")
    parser.add_argument("--input", type=Path, required=True, help="Path to proof payload JSON.")
    parser.add_argument("--supabase-table", default=settings.supabase_proof_qa_table, help="Supabase table for audits.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=settings.proof_qa_concurrency,
        help="Number of concurrent HTTP checks.",
    )
    parser.add_argument(
        "--retry-limit",
        type=int,
        default=settings.proof_qa_retry_limit,
        help="Total attempts (including first try) per URL.",
    )
    parser.add_argument(
        "--failure-threshold",
        type=float,
        default=settings.proof_qa_failure_threshold,
        help="Failure rate threshold that triggers alerts.",
    )
    parser.add_argument(
        "--alert-webhook",
        default=settings.proof_qa_alert_webhook,
        help="Webhook for failure alerts.",
    )
    return parser.parse_args(argv)


def load_proof_targets(path: Path) -> list[ProofCheckTarget]:
    if not path.exists():
        raise ProofLinkMonitorError(f"Input file not found: {path}", code="404_INPUT_NOT_FOUND")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProofLinkMonitorError(f"Invalid JSON: {exc}", code="422_INVALID_JSON") from exc

    bundle_id = payload.get("bundle_id") if isinstance(payload, Mapping) else None
    records: list[ProofCheckTarget] = []

    if isinstance(payload, Mapping) and "leads" in payload:
        for lead in payload.get("leads", []):
            records.extend(_records_from_lead(lead, bundle_id))
        return records

    items: Sequence[Mapping[str, Any]] = []
    if isinstance(payload, list):
        items = payload  # type: ignore[assignment]
    elif isinstance(payload, Mapping):
        for key in ("proofs", "records", "signals"):
            if key in payload and isinstance(payload[key], list):
                items = payload[key]  # type: ignore[assignment]
                break
        if not items:
            items = [payload]  # type: ignore[list-item]
    else:
        raise ProofLinkMonitorError("Unsupported input payload.", code="422_INVALID_PROOF_PAYLOAD")

    for item in items:
        record = _record_from_payload(item, bundle_id)
        if record:
            records.append(record)
    return records


def _records_from_lead(payload: Mapping[str, Any], bundle_id: str | None) -> list[ProofCheckTarget]:
    if not isinstance(payload, Mapping):
        raise ProofLinkMonitorError("Lead entries must be objects.", code="422_INVALID_PROOF_PAYLOAD")
    company_id = payload.get("company_id")
    company = payload.get("company")
    slug = payload.get("slug")
    verified_by = payload.get("verified_by") or []
    proof_links = payload.get("proof_links") or []
    if not isinstance(proof_links, Sequence) or isinstance(proof_links, (str, bytes)):
        raise ProofLinkMonitorError("Lead proof_links must be a list.", code="422_INVALID_PROOF_PAYLOAD")
    proofs: list[ProofCheckTarget] = []
    for url in proof_links:
        record = _build_target(
            {
                "company_id": company_id,
                "company_name": company,
                "slug": slug,
                "source_url": url,
                "verified_by": verified_by,
            },
            bundle_id=bundle_id,
        )
        if record:
            proofs.append(record)
    return proofs


def _record_from_payload(payload: Mapping[str, Any], bundle_id: str | None) -> ProofCheckTarget | None:
    if not isinstance(payload, Mapping):
        raise ProofLinkMonitorError("Proof entries must be objects.", code="422_INVALID_PROOF_PAYLOAD")
    record = _build_target(payload, bundle_id=bundle_id)
    return record


def _build_target(payload: Mapping[str, Any], *, bundle_id: str | None) -> ProofCheckTarget | None:
    source_url = payload.get("source_url")
    if not source_url:
        return None
    raw_timestamp = payload.get("timestamp")
    timestamp = _parse_timestamp(raw_timestamp)
    if raw_timestamp is not None and timestamp is None:
        raise ProofLinkMonitorError(
            "Invalid proof timestamp provided.",
            code="422_INVALID_PROOF_PAYLOAD",
        )
    if timestamp is None:
        # Leads exports omit timestamps; reuse available metadata or fall back to "now".
        timestamp = _fallback_timestamp(payload)
    try:
        proof = SignalProof(
            source_url=source_url,
            verified_by=payload.get("verified_by") or [],
            timestamp=timestamp,
            proof_hash=payload.get("proof_hash"),
        )
    except ValidationError as exc:
        raise ProofLinkMonitorError(f"Invalid proof payload: {exc}", code="422_INVALID_PROOF_PAYLOAD") from exc

    sanitized_url = sanitize_proof_url(str(proof.source_url))
    return ProofCheckTarget(
        proof_hash=proof.proof_hash or "",
        source_url=sanitized_url,
        company_id=str(payload.get("company_id")) if payload.get("company_id") else None,
        company_name=payload.get("company_name") or payload.get("company"),
        slug=payload.get("slug"),
        bundle_id=bundle_id or payload.get("bundle_id"),
        verified_by=proof.verified_by,
        timestamp=proof.timestamp,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning("proof_qa.timestamp_parse_failed", extra={"value": value})
        return None


def _fallback_timestamp(payload: Mapping[str, Any]) -> datetime:
    for field in ("captured_at", "bundle_captured_at", "created_at"):
        fallback_value = payload.get(field)
        parsed = _parse_timestamp(fallback_value)
        if parsed:
            return parsed
    return datetime.now(UTC)


async def _run_async(args: argparse.Namespace, targets: list[ProofCheckTarget]) -> ProofMonitorSummary:
    supabase_url = settings.supabase_url
    service_key = settings.supabase_service_key
    table = args.supabase_table
    if not supabase_url or not service_key or not table:
        raise ProofLinkMonitorError(
            "Supabase configuration missing (SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_PROOF_QA_TABLE).",
            code="E_SUPABASE_CONFIG",
        )

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http_client, httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as supabase_client, httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as alert_client:
        audit_store = SupabaseAuditClient(
            base_url=supabase_url,
            service_key=service_key,
            table=table,
            http_client=supabase_client,
        )
        alert_publisher = AlertDispatcher(
            webhook_url=args.alert_webhook,
            disabled=settings.proof_qa_disable_alerts,
            http_client=alert_client,
        )
        monitor = ProofLinkMonitor(
            http_client=http_client,
            audit_store=audit_store,
            alert_publisher=alert_publisher,
            concurrency=args.concurrency,
            retry_limit=args.retry_limit,
            failure_threshold=args.failure_threshold,
        )
        return await monitor.run(targets)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    targets = load_proof_targets(args.input)
    try:
        summary = asyncio.run(_run_async(args, targets))
    except ProofLinkMonitorError as exc:
        logger.error("proof_qa.run_failed", extra={"code": exc.code, "message": str(exc)})
        raise
    logger.info(
        "proof_qa.completed",
        extra={
            "checks": summary.total,
            "failures": summary.failures,
            "failure_rate": f"{summary.failure_rate:.2%}",
        },
    )


if __name__ == "__main__":
    try:
        main()
    except ProofLinkMonitorError:
        raise SystemExit(1)
