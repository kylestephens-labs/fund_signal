from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from app.main import app
from app.services.scoring.chatgpt_engine import (
    ChatGPTScoringEngine,
    ScoringContext,
    get_scoring_engine,
)
from app.services.scoring.proof_links import ProofLinkHydrator
from app.services.scoring.repositories import InMemoryScoreRepository


def _build_engine() -> ChatGPTScoringEngine:
    return ChatGPTScoringEngine(
        repository=InMemoryScoreRepository(),
        context=ScoringContext(
            mode="fixture",
            system_prompt="system",
            model="fixture-rubric",
            temperature=0.0,
        ),
        proof_hydrator=ProofLinkHydrator(default_sources={}),
    )


def _sample_payload(company_id: str, scoring_run_id: str) -> dict[str, object]:
    return {
        "company_id": company_id,
        "name": "API Test Co",
        "funding_amount": "$10M",
        "funding_stage": "Series A",
        "days_since_funding": 60,
        "employee_count": 40,
        "job_postings": 5,
        "tech_stack": ["Salesforce"],
        "buying_signals": ["https://example.com/news"],
        "verified_sources": ["Exa", "Tavily"],
        "scoring_run_id": scoring_run_id,
    }


@contextmanager
def _override_engine(engine: ChatGPTScoringEngine):
    app.dependency_overrides[get_scoring_engine] = lambda: engine
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_scoring_engine, None)


def test_create_and_fetch_score_round_trip(client):
    engine = _build_engine()
    company_id = str(uuid4())
    run_id = "daily-api-test"
    payload = _sample_payload(company_id, run_id)
    with _override_engine(engine):
        response = client.post("/api/scores", json=payload)
        assert response.status_code == 201
        created = response.json()
        assert created["company_id"] == company_id
        assert created["scoring_run_id"] == run_id

        listing = client.get(f"/api/scores/{company_id}?scoring_run_id={run_id}")
        assert listing.status_code == 200
        results = listing.json()
        assert isinstance(results, list)
        assert results and results[0]["scoring_run_id"] == run_id


def test_get_missing_score_returns_404(client):
    engine = _build_engine()
    company_id = str(uuid4())
    with _override_engine(engine):
        response = client.get(f"/api/scores/{company_id}?scoring_run_id=missing-run")
        assert response.status_code == 404


def test_list_scores_for_run_enforces_limit(client):
    engine = _build_engine()
    run_id = "daily-ui-feed"
    with _override_engine(engine):
        for _ in range(2):
            company_id = str(uuid4())
            payload = _sample_payload(company_id, run_id)
            response = client.post("/api/scores", json=payload)
            assert response.status_code == 201

        listing = client.get(f"/api/scores?scoring_run_id={run_id}&limit=1")
        assert listing.status_code == 200
        results = listing.json()
        assert len(results) == 1
        assert results[0]["scoring_run_id"] == run_id


def test_list_scores_for_run_requires_scoring_run_id(client):
    engine = _build_engine()
    with _override_engine(engine):
        response = client.get("/api/scores")
        assert response.status_code == 400


def test_list_scores_for_run_returns_404_when_missing(client):
    engine = _build_engine()
    with _override_engine(engine):
        response = client.get("/api/scores?scoring_run_id=unknown-run")
        assert response.status_code == 404
