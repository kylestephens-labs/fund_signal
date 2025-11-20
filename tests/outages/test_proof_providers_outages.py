from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.clients.exa import ExaError
from app.models.company import CompanyProfile
from app.models.signal_breakdown import SignalEvidence
from app.services.scoring.chatgpt_engine import ChatGPTScoringEngine, ScoringEngineError
from app.services.scoring.proof_links import ProofLinkHydrator
from app.services.scoring.repositories import InMemoryScoreRepository
from pipelines.day1 import exa_discovery, tavily_confirm, youcom_verify
from tests.outages.fake_providers import (
    FakeExaClient,
    FakeTavilyClient,
    FakeYoucomClient,
    ProviderOutageScenario,
)

pytestmark = pytest.mark.slow


class SleepRecorder:
    """Helper to capture exponential backoff sleeps without slowing tests."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _company(**overrides) -> CompanyProfile:
    payload = {
        "company_id": uuid4(),
        "name": "Fixture Corp",
        "funding_amount": "$12M",
        "funding_stage": "Series A",
        "days_since_funding": 45,
        "employee_count": 80,
        "job_postings": 6,
        "tech_stack": ["HubSpot"],
        "buying_signals": ["https://news.dev/acme"],
        "verified_sources": ["Exa", "Tavily"],
        "signals": [],
    }
    payload.update(overrides)
    return CompanyProfile(**payload)


def test_youcom_timeout_retries_emit_structured_event(caplog):
    caplog.set_level(logging.WARNING, logger="pipelines.day1.youcom_verify")
    scenario = ProviderOutageScenario(provider="youcom", mode="timeout", attempts_before_success=2)
    client = FakeYoucomClient(scenario)
    sleeper = SleepRecorder()

    articles = youcom_verify.discover_with_retries(
        client,
        query="Acme Series A",
        limit=3,
        max_attempts=5,
        sleep=sleeper,
    )

    assert len(articles) == 1
    assert client.calls == 3
    assert len(sleeper.delays) == 2
    retry_logs = [record for record in caplog.records if record.getMessage() == "provider.retry"]
    assert len(retry_logs) == 2
    assert retry_logs[0].provider == "youcom"
    assert retry_logs[0].code == "YOUCOM_TIMEOUT"
    assert retry_logs[0].attempt == 1
    assert retry_logs[0].delay_ms > 0

    signal = SignalEvidence(
        slug="funding",
        source_url=articles[0].url,
        timestamp=datetime.now(UTC),
        verified_by=["You.com"],
    )
    hydrator = ProofLinkHydrator(default_sources={})
    company = _company(signals=[signal])
    hydrator.hydrate(company, "funding")
    hydrator.hydrate(company, "funding")
    stats = hydrator.cache_stats
    assert stats["misses"] == 1
    assert stats["hits"] == 1


def test_tavily_slow_response_records_latency():
    scenario = ProviderOutageScenario(provider="tavily", mode="slow", delay_ms=1200)
    client = FakeTavilyClient(scenario)

    results = tavily_confirm.discover_with_retries(
        client,
        query="Fixture",
        max_results=2,
        max_attempts=3,
    )

    assert len(results) == 1
    assert client.calls == 1
    assert client.observed_latencies, "Expected the slow fake to record latency."
    assert client.observed_latencies[0] >= 1.0


def test_exa_server_error_bubbles_with_code(caplog):
    caplog.set_level(logging.WARNING, logger="pipelines.day1.exa_discovery")
    scenario = ProviderOutageScenario(provider="exa", mode="server_error", status_code=503)
    client = FakeExaClient(scenario)

    with pytest.raises(ExaError) as excinfo:
        exa_discovery.discover_with_retries(
            client,
            query="Fixture",
            days_min=10,
            days_max=30,
            limit=5,
            max_attempts=2,
        )

    assert excinfo.value.code == "EXA_5XX"
    assert not any(record.getMessage() == "provider.retry" for record in caplog.records)


def test_proof_hydrator_logs_success_on_fallback(caplog):
    caplog.set_level(logging.INFO, logger="app.services.scoring.proof_links")
    hydrator = ProofLinkHydrator(default_sources={"funding": "https://fallback.local/funding"})
    company = _company(buying_signals=["https://news.dev/acme?token=123"], signals=[])

    proof = hydrator.hydrate(company, "funding")

    assert str(proof.source_url) == "https://news.dev/acme"
    outage_logs = [
        record for record in caplog.records if record.getMessage() == "proof_hydrator.outage_sim"
    ]
    assert outage_logs, "expected outage log entry"
    last = outage_logs[-1]
    assert last.status == "success"
    assert last.slug == "funding"
    assert last.attempts >= 1
    assert last.proof_count == 1
    assert last.latency_ms >= 0


def test_scoring_engine_surfaces_missing_proof_outage(caplog):
    caplog.set_level(logging.INFO, logger="app.services.scoring.proof_links")
    hydrator = ProofLinkHydrator(default_sources={})
    engine = ChatGPTScoringEngine(repository=InMemoryScoreRepository(), proof_hydrator=hydrator)
    company = _company(buying_signals=[], signals=[])

    with pytest.raises(ScoringEngineError) as excinfo:
        engine.score_company(company, scoring_run_id="outage-1", force=True)

    assert excinfo.value.code == "404_PROOF_NOT_FOUND"
    outage_logs = [
        record for record in caplog.records if record.getMessage() == "proof_hydrator.outage_sim"
    ]
    assert outage_logs, "expected outage log entry"
    last = outage_logs[-1]
    assert last.status == "error"
    assert last.slug == "funding"
    assert last.error_code == "404_PROOF_NOT_FOUND"
