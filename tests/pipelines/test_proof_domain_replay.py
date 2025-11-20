from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

import pipelines.day3 as day3_module
from app.config import settings
from app.models.company import BreakdownItem, CompanyScore
from app.models.signal_breakdown import SignalProof
from app.services.scoring.repositories import InMemoryScoreRepository
from pipelines.day3.email_delivery import render_email
from pipelines.day3.slack_delivery import build_slack_payload
from pipelines.qa import proof_domain_replay as replay

pytestmark = pytest.mark.slow


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


# --- Day-3 delivery helpers -------------------------------------------------


def _delivery_score(*, score: int, run_id: str = "demo-run") -> CompanyScore:
    proof = SignalProof(
        source_url="https://news.example.com/proof",
        verified_by=["Exa", "Tavily"],
        timestamp=datetime.now(UTC),
    )
    breakdown = [
        BreakdownItem(
            reason="Funding momentum",
            points=score,
            proof=proof,
            proofs=[proof],
        )
    ]
    return CompanyScore(
        company_id=uuid4(),
        score=score,
        breakdown=breakdown,
        recommended_approach="Reach out with a tailored email.",
        pitch_angle="Help them convert the new round into pipeline.",
        scoring_model="fixture",
        scoring_run_id=run_id,
    )


def test_day3_fetch_scores_sorted(monkeypatch: pytest.MonkeyPatch):
    repo = InMemoryScoreRepository()
    repo.save(_delivery_score(score=88))
    repo.save(_delivery_score(score=92))
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///memory", raising=False)
    monkeypatch.setattr(day3_module, "build_score_repository", lambda: repo, raising=False)

    results = day3_module.fetch_scores_for_delivery("demo-run", limit=1)

    assert len(results) == 1
    assert results[0].score == 92


def test_day3_in_memory_repository_limit_zero():
    repo = InMemoryScoreRepository()
    repo.save(_delivery_score(score=55))
    repo.save(_delivery_score(score=65))

    assert repo.list_run("demo-run", limit=0) == []


def test_email_renderer_includes_proofs():
    run_id = "demo-run"
    score = _delivery_score(score=84, run_id=run_id)

    payload = render_email(run_id, [score])

    assert str(score.company_id) in payload
    assert "https://news.example.com/proof" in payload
    assert run_id in payload


def test_slack_payload_tracks_metadata():
    scores = [_delivery_score(score=90), _delivery_score(score=76)]

    payload = build_slack_payload("demo-run", scores, webhook_url="https://hooks.slack.test")

    assert payload["metadata"]["company_count"] == 2
    assert payload["metadata"]["scores"][0]["score"] == 90
    assert any(
        "<https://news.example.com/proof|" in block["text"]["text"]
        for block in payload["blocks"]
        if block["type"] == "section"
    )
