from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.config import settings
from app.models.company import BreakdownItem, CompanyScore
from app.models.signal_breakdown import SignalProof
from pipelines.day3 import DeliveryError, fetch_scores_for_delivery


def _sample_score(score: int = 82) -> CompanyScore:
    proof = SignalProof(
        source_url="https://news.example.com/proof",
        verified_by=["Exa"],
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
        recommended_approach="Email the VP of Sales.",
        pitch_angle="Help them convert capital into pipeline.",
        scoring_model="fixture",
        scoring_run_id="demo-day3",
    )


def test_fetch_scores_logs_metrics_with_repo(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    captured: dict[str, dict] = {}

    class StubRepo:
        def list_run(self, scoring_run_id: str, limit: int | None = None):  # noqa: D401, ANN001
            return [_sample_score()]

    def _increment(metric: str, *, tags=None):  # noqa: ANN001
        captured["metric"] = {"name": metric, "tags": tags}

    monkeypatch.setattr(settings, "database_url", "postgresql://stub", raising=False)
    monkeypatch.setattr("pipelines.day3.metrics.increment", _increment)
    caplog.set_level("INFO", logger="pipelines.day3")

    scores = fetch_scores_for_delivery("demo-day3", repository=StubRepo())

    assert scores and scores[0].score == 82
    assert captured["metric"]["name"] == "delivery.supabase.query"
    assert captured["metric"]["tags"]["scoring_run"] == "demo-day3"
    assert any(record.message == "delivery.supabase.query" for record in caplog.records)


def test_fetch_scores_requires_database_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "database_url", None, raising=False)
    with pytest.raises(DeliveryError) as excinfo:
        fetch_scores_for_delivery("demo-day3")
    assert excinfo.value.code == "E_DATABASE_URL_MISSING"
