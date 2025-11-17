from __future__ import annotations

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


def test_create_and_fetch_score_round_trip(client):
    engine = _build_engine()
    app.dependency_overrides[get_scoring_engine] = lambda: engine
    company_id = str(uuid4())
    run_id = "daily-api-test"
    payload = _sample_payload(company_id, run_id)
    try:
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
    finally:
        app.dependency_overrides.pop(get_scoring_engine, None)


def test_get_missing_score_returns_404(client):
    engine = _build_engine()
    app.dependency_overrides[get_scoring_engine] = lambda: engine
    company_id = str(uuid4())
    try:
        response = client.get(f"/api/scores/{company_id}?scoring_run_id=missing-run")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_scoring_engine, None)
