from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
import pytest

from app.models.company import CompanyProfile
from app.models.signal_breakdown import SignalEvidence
from app.services.scoring import proof_links as proof_links_module
from app.services.scoring.proof_links import ProofLinkError, ProofLinkHydrator
from tools import proof_links_load_test
from tests.helpers.metrics_stub import StubMetrics


def _recent_timestamp(offset_days: int = 5) -> datetime:
    return datetime.now(UTC) - timedelta(days=offset_days)


def _company(**overrides) -> CompanyProfile:
    payload = {
        "company_id": uuid4(),
        "name": "Proof Corp",
        "funding_amount": "$5M",
        "funding_stage": "Seed",
        "days_since_funding": 30,
        "employee_count": 25,
        "job_postings": 3,
        "tech_stack": ["HubSpot"],
        "buying_signals": ["http://news.dev/proof?token=abc123"],
        "verified_sources": ["Exa", "You.com"],
        "signals": [],
    }
    payload.update(overrides)
    return CompanyProfile(**payload)



def test_hydrate_prefers_structured_signal_metadata():
    fresh_timestamp = _recent_timestamp()
    evidence = SignalEvidence(
        slug="funding",
        source_url="http://techcrunch.com/acme?api_key=secret&view=1",
        timestamp=fresh_timestamp,
        verified_by=["Exa", "Tavily", "exa"],
        source_hint="TechCrunch",
    )
    company = _company(signals=[evidence])
    hydrator = ProofLinkHydrator(default_sources={"funding": "https://fallback.local/funding"})

    proof = hydrator.hydrate(company, "funding")

    assert str(proof.source_url) == "https://techcrunch.com/acme?view=1"
    assert proof.verified_by == ["Exa", "Tavily"]
    assert proof.timestamp == fresh_timestamp
    assert proof.proof_hash


def test_hydrate_falls_back_to_buying_signal_with_sanitized_url():
    company = _company(signals=[])
    hydrator = ProofLinkHydrator(default_sources={"team": "https://fallback/team"})

    proof = hydrator.hydrate(company, "team")

    assert str(proof.source_url) == "https://news.dev/proof"
    assert proof.verified_by == ["Exa", "You.com"]


def test_missing_slug_without_fallback_raises():
    company = _company(buying_signals=[])
    hydrator = ProofLinkHydrator(default_sources={})

    with pytest.raises(ProofLinkError) as excinfo:
        hydrator.hydrate(company, "unknown")

    assert excinfo.value.code == "404_PROOF_NOT_FOUND"


def test_cache_hits_are_tracked() -> None:
    evidence = SignalEvidence(
        slug="signals",
        source_url="https://exa.ai/record/acme",
        timestamp=_recent_timestamp(),
        verified_by=["Exa"],
    )
    company = _company(signals=[evidence])
    hydrator = ProofLinkHydrator(default_sources={})

    first = hydrator.hydrate(company, "signals")
    second = hydrator.hydrate(company, "signals")

    assert first is second
    assert hydrator.cache_stats["hits"] == 1


def test_default_sources_are_normalized_once() -> None:
    company = _company(buying_signals=[])
    hydrator = ProofLinkHydrator(default_sources={"funding": "http://signal.test/proof?token=abc"})

    proof = hydrator.hydrate(company, "funding")

    assert str(proof.source_url) == "https://signal.test/proof"


def test_hydrate_many_returns_all_buying_signals_sanitized():
    company = _company(
        signals=[],
        buying_signals=[
            "http://news.dev/proof?token=abc123",
            "https://news.dev/alt?key=456",
            "http://news.dev/proof?token=abc123",
        ],
    )
    hydrator = ProofLinkHydrator(default_sources={})

    proofs = hydrator.hydrate_many(company, "team")

    assert [str(proof.source_url) for proof in proofs] == [
        "https://news.dev/proof",
        "https://news.dev/alt",
    ]


def test_hydrate_many_respects_limit():
    company = _company(
        signals=[],
        buying_signals=[
            "https://news.dev/one",
            "https://news.dev/two",
        ],
    )
    hydrator = ProofLinkHydrator(default_sources={})

    proofs = hydrator.hydrate_many(company, "signals", limit=1)

    assert len(proofs) == 1
    assert str(proofs[0].source_url) == "https://news.dev/one"


def test_structured_evidence_multiple_entries():
    first_ts = _recent_timestamp(6)
    second_ts = _recent_timestamp(5)
    evidence = [
        SignalEvidence(
            slug="funding",
            source_url="https://techcrunch.com/acme",
            timestamp=first_ts,
            verified_by=["Exa"],
            source_hint="TechCrunch",
            proof_hash="funding-1",
        ),
        SignalEvidence(
            slug="funding",
            source_url="https://press.dev/acme?token=secret",
            timestamp=second_ts,
            verified_by=["You.com"],
            source_hint="PressWire",
            proof_hash="funding-2",
        ),
    ]
    company = _company(signals=evidence)
    hydrator = ProofLinkHydrator(default_sources={})

    proofs = hydrator.hydrate_many(company, "funding")

    assert [str(proof.source_url) for proof in proofs] == [
        "https://techcrunch.com/acme",
        "https://press.dev/acme",
    ]


def test_missing_timestamp_raises_proof_error():
    evidence = SignalEvidence(
        slug="funding",
        source_url="https://techcrunch.com/acme",
        timestamp=None,
        verified_by=["Exa"],
    )
    company = _company(signals=[evidence])
    hydrator = ProofLinkHydrator(default_sources={})

    with pytest.raises(ProofLinkError) as excinfo:
        hydrator.hydrate(company, "funding")

    assert excinfo.value.code == "422_PROOF_MISSING_TIMESTAMP"


def test_stale_proof_logs_and_raises(monkeypatch):
    stale_timestamp = datetime.now(UTC) - timedelta(days=200)
    evidence = SignalEvidence(
        slug="funding",
        source_url="https://techcrunch.com/acme",
        timestamp=stale_timestamp,
        verified_by=["Exa"],
    )
    company = _company(signals=[evidence])
    hydrator = ProofLinkHydrator(default_sources={})
    monkeypatch.setattr(proof_links_module.settings, "proof_max_age_days", 90)

    with pytest.raises(ProofLinkError) as excinfo:
        hydrator.hydrate(company, "funding")

    assert excinfo.value.code == "422_PROOF_STALE"


def test_proof_link_load_harness_generates_metrics(monkeypatch):
    monkeypatch.setenv("FUND_SIGNAL_MODE", "fixture")
    fixture_path = Path("tests/fixtures/scoring/regression_companies.json")
    config = proof_links_load_test.LoadTestConfig(
        companies_path=fixture_path,
        concurrency=2,
        iterations=2,
        warm_cache=True,
        report_path=None,
        force_report=False,
        scoring_run_id="unit-load",
        p95_threshold_ms=500.0,
    )

    result = proof_links_load_test.run_load_test(config)

    assert result["metadata"]["companies_count"] >= 1
    assert result["cache_stats"]["hits"] >= 0
    assert result["latency_ms"]["hydrator"]["overall"]["p95"] < 500


def test_hydrator_emits_metrics(monkeypatch):
    stub = StubMetrics()
    monkeypatch.setattr(proof_links_module, "metrics", stub)
    fresh_timestamp = _recent_timestamp()
    evidence = SignalEvidence(
        slug="funding",
        source_url="https://techcrunch.com/acme",
        timestamp=fresh_timestamp,
        verified_by=["Exa"],
    )
    company = _company(signals=[evidence])
    hydrator = ProofLinkHydrator(default_sources={})

    hydrator.hydrate(company, "funding")
    hydrator.hydrate(company, "funding")

    assert any(call["metric"].endswith("hydrator.latency_ms") for call in stub.timing_calls)
    assert any(call["metric"].endswith("hydrator.cache_miss") for call in stub.increment_calls)
    assert any(call["metric"].endswith("hydrator.cache_hit") for call in stub.increment_calls)
    assert any(call["metric"].endswith("hydrator.proof_count") for call in stub.gauge_calls)


def test_proof_link_load_harness_cli_threshold(monkeypatch):
    monkeypatch.setenv("FUND_SIGNAL_MODE", "fixture")
    fake_result = {
        "latency_ms": {"hydrator": {"overall": {"p95": 1.0}}},
        "cache_stats": {"hit_ratio": 0.5},
        "throughput_qps": 10.0,
        "error_summary": {},
        "metadata": {"companies_count": 1, "iterations": 1},
        "score_successes": 1,
        "errors": [],
    }
    monkeypatch.setattr(proof_links_load_test, "run_load_test", lambda config: fake_result)
    monkeypatch.setattr(proof_links_load_test, "_emit_summary_logs", lambda result: None)
    args = [
        "--input",
        "tests/fixtures/scoring/regression_companies.json",
        "--iterations",
        "1",
        "--concurrency",
        "1",
        "--p95-threshold-ms",
        "0.0001",
        "--no-warm-cache",
    ]

    exit_code = proof_links_load_test.main(args)

    assert exit_code == 2
