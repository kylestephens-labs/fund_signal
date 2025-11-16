from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from pipelines.qa import proof_domain_replay as replay


def _sample_score(**overrides):
    base = {
        "company_id": "11111111-0000-0000-0000-000000000001",
        "company_name": "Acme Corp",
        "score": 80,
        "breakdown": [
            {
                "reason": "Funding momentum",
                "points": 30,
                "proofs": [
                    {
                        "source_url": "https://news.example.com/acme",
                        "verified_by": ["Exa"],
                        "timestamp": "2025-11-05T12:00:00Z",
                    }
                ],
            },
            {
                "reason": "Hiring velocity",
                "points": 20,
                "proofs": [
                    {
                        "source_url": "https://news.example.com/acme",
                        "verified_by": ["Exa"],
                        "timestamp": "2025-11-05T12:00:00Z",
                    }
                ],
            },
        ],
        "recommended_approach": "Email the VP of Sales.",
        "pitch_angle": "Help them scale GTM.",
        "scoring_model": "fixture",
        "scoring_run_id": "run-1",
    }
    base.update(overrides)
    return base


def test_load_scores_dedupes_proofs(tmp_path: Path):
    payload = [_sample_score()]
    scores_path = tmp_path / "scores.json"
    scores_path.write_text(json.dumps(payload), encoding="utf-8")

    targets = replay.load_scores(scores_path)

    assert len(targets) == 1
    assert targets[0].company_name == "Acme Corp"


def test_load_scores_keeps_same_proof_for_different_companies(tmp_path: Path):
    payload = [
        _sample_score(company_id="11111111-0000-0000-0000-000000000001", company_name="Acme Corp"),
        _sample_score(company_id="22222222-0000-0000-0000-000000000002", company_name="Beta Corp"),
    ]
    scores_path = tmp_path / "scores-diff.json"
    scores_path.write_text(json.dumps(payload), encoding="utf-8")

    targets = replay.load_scores(scores_path)

    assert len(targets) == 2
    assert {target.company_name for target in targets} == {"Acme Corp", "Beta Corp"}


class _StubStore(replay.ReplayAuditStore):
    def __init__(self) -> None:
        self.rows: list[replay.ReplayAuditRow] = []

    async def upsert(self, rows):
        self.rows.extend(rows)


class _StubAlerts(replay.AlertPublisher):
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def publish(self, category, payload):
        self.events.append((category, dict(payload)))


def _target(url: str) -> replay.ReplayTarget:
    return replay.ReplayTarget(
        proof_hash="proof-123",
        source_url=url,
        company_id="c-1",
        company_name="Acme",
        slug="funding",
        verified_by=["Exa"],
        scoring_run_id="run-1",
    )


@pytest.mark.asyncio
async def test_replay_flags_protocol_downgrade():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://news.example.com/acme"):
            return httpx.Response(301, headers={"location": "http://malicious.example.com"})
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    store = _StubStore()
    alerts = _StubAlerts()

    async with httpx.AsyncClient(transport=transport) as client:
        job = replay.ProofDomainReplay(
            http_client=client,
            audit_store=store,
            alert_publisher=alerts,
            concurrency=1,
            max_redirects=3,
            failure_threshold=1.0,
            bundle_id="bundle-1",
            replay_run_id="replay-1",
        )
        summary = await job.run([_target("https://news.example.com/acme")])

    assert summary.failures == 1
    assert store.rows[0].protocol_downgraded is True
    assert alerts.events[0][0] == "insecure_redirect"


@pytest.mark.asyncio
async def test_replay_succeeds_without_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    store = _StubStore()
    alerts = _StubAlerts()

    async with httpx.AsyncClient(transport=transport) as client:
        job = replay.ProofDomainReplay(
            http_client=client,
            audit_store=store,
            alert_publisher=alerts,
            concurrency=1,
            max_redirects=2,
            failure_threshold=1.0,
            bundle_id=None,
            replay_run_id=None,
        )
        summary = await job.run([_target("https://news.example.com/acme")])

    assert summary.failures == 0
    assert alerts.events == []
    assert store.rows[0].final_url == "https://news.example.com/acme"
