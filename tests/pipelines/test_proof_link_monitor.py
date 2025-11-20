from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from pipelines.qa import proof_link_monitor as monitor

pytestmark = pytest.mark.slow


def test_load_proof_targets_supports_leads_payload(tmp_path: Path):
    payload = {
        "bundle_id": "bundle-123",
        "leads": [
            {
                "company_id": "111",
                "company": "Acme Corp",
                "slug": "funding",
                "verified_by": ["Exa"],
                "proof_links": [
                    "https://news.example.com/acme",
                    "https://news.example.com/acme",
                ],
            }
        ],
    }
    input_path = tmp_path / "leads.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    targets = monitor.load_proof_targets(input_path)

    assert len(targets) == 2
    assert targets[0].company_name == "Acme Corp"
    assert targets[0].bundle_id == "bundle-123"
    assert targets[0].timestamp is not None


def test_load_proof_targets_allows_null_proof_links(tmp_path: Path):
    payload = {
        "leads": [
            {
                "company_id": "c-1",
                "company": "Acme Corp",
                "slug": "funding",
                "proof_links": None,
            }
        ]
    }
    input_path = tmp_path / "leads-null.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    targets = monitor.load_proof_targets(input_path)

    assert targets == []


def test_load_proof_targets_rejects_non_mapping_lead(tmp_path: Path):
    payload = {"leads": ["https://example.com/not-a-dict"]}
    input_path = tmp_path / "invalid-lead.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(monitor.ProofLinkMonitorError) as excinfo:
        monitor.load_proof_targets(input_path)

    assert excinfo.value.code == "422_INVALID_PROOF_PAYLOAD"


def test_load_proof_targets_rejects_non_mapping_record(tmp_path: Path):
    payload = ["https://example.com/not-a-dict"]
    input_path = tmp_path / "invalid-record.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(monitor.ProofLinkMonitorError) as excinfo:
        monitor.load_proof_targets(input_path)

    assert excinfo.value.code == "422_INVALID_PROOF_PAYLOAD"


def test_load_proof_targets_rejects_invalid_timestamp(tmp_path: Path):
    payload = [
        {
            "source_url": "https://news.example.com/acme",
            "timestamp": "not-a-date",
        }
    ]
    input_path = tmp_path / "invalid-timestamp.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(monitor.ProofLinkMonitorError) as excinfo:
        monitor.load_proof_targets(input_path)

    assert excinfo.value.code == "422_INVALID_PROOF_PAYLOAD"


class _StubAuditStore:
    def __init__(self, previous: dict[str, monitor.ProofAuditState] | None = None) -> None:
        self.previous = previous or {}
        self.rows: list[monitor.ProofAuditRow] = []

    async def upsert(self, rows):
        self.rows.extend(rows)

    async def fetch_latest(self, proof_hashes):
        return {key: value for key, value in self.previous.items() if key in proof_hashes}


class _StubAlertPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def publish(self, category, payload):
        self.events.append((category, dict(payload)))


def _target(slug: str, url: str) -> monitor.ProofCheckTarget:
    return monitor.ProofCheckTarget(
        proof_hash=f"{slug}-hash",
        source_url=url,
        company_id="c-1",
        company_name="Acme",
        slug=slug,
        bundle_id="bundle-1",
        verified_by=["Exa"],
        timestamp=None,
    )


@pytest.mark.asyncio
async def test_monitor_dedupes_urls_and_persists():
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    audit_store = _StubAuditStore()
    alerts = _StubAlertPublisher()

    async with httpx.AsyncClient(transport=transport) as client:
        monitor_instance = monitor.ProofLinkMonitor(
            http_client=client,
            audit_store=audit_store,
            alert_publisher=alerts,
            concurrency=5,
            retry_limit=2,
            failure_threshold=0.9,
        )
        summary = await monitor_instance.run(
            [
                _target("funding", "https://news.example.com/acme"),
                _target("tech", "https://news.example.com/acme"),
            ]
        )

    assert summary.total == 2
    assert audit_store.rows[0].http_status == 200
    assert seen_methods.count("HEAD") == 1  # deduped network call
    assert alerts.events == []


@pytest.mark.asyncio
async def test_monitor_alerts_on_repeat_failure():
    previous = monitor.ProofAuditState(
        proof_hash="funding-hash",
        http_status=404,
        last_checked_at=datetime.now(UTC) - timedelta(hours=1),
        last_success_at=None,
    )
    audit_store = _StubAuditStore(previous={"funding-hash": previous})
    alerts = _StubAlertPublisher()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as client:
        monitor_instance = monitor.ProofLinkMonitor(
            http_client=client,
            audit_store=audit_store,
            alert_publisher=alerts,
            concurrency=2,
            retry_limit=1,
            failure_threshold=1.0,
        )
        summary = await monitor_instance.run([_target("funding", "https://news.example.com/acme")])

    assert summary.failures == 1
    assert alerts.events[0][0] == "repeat_failure"


@pytest.mark.asyncio
async def test_monitor_raises_when_failure_threshold_exceeded():
    audit_store = _StubAuditStore()
    alerts = _StubAlertPublisher()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as client:
        monitor_instance = monitor.ProofLinkMonitor(
            http_client=client,
            audit_store=audit_store,
            alert_publisher=alerts,
            concurrency=1,
            retry_limit=1,
            failure_threshold=0.1,
        )
        with pytest.raises(monitor.ProofLinkMonitorError):
            await monitor_instance.run([_target("funding", "https://news.example.com/acme")])

    assert alerts.events[0][0] == "failure_rate"
