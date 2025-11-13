from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.company import CompanyProfile
from app.models.signal_breakdown import SignalEvidence
from app.services.scoring.proof_links import ProofLinkError, ProofLinkHydrator


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
    evidence = SignalEvidence(
        slug="funding",
        source_url="http://techcrunch.com/acme?api_key=secret&view=1",
        timestamp=datetime(2025, 1, 5, 12, 0, tzinfo=UTC),
        verified_by=["Exa", "Tavily", "exa"],
        source_hint="TechCrunch",
    )
    company = _company(signals=[evidence])
    hydrator = ProofLinkHydrator(default_sources={"funding": "https://fallback.local/funding"})

    proof = hydrator.hydrate(company, "funding")

    assert str(proof.source_url) == "https://techcrunch.com/acme?view=1"
    assert proof.verified_by == ["Exa", "Tavily"]
    assert proof.timestamp == datetime(2025, 1, 5, 12, 0, tzinfo=UTC)
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
        timestamp=datetime(2025, 1, 5, 12, 0, tzinfo=UTC),
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
    evidence = [
        SignalEvidence(
            slug="funding",
            source_url="https://techcrunch.com/acme",
            timestamp=datetime(2025, 1, 5, 12, 0, tzinfo=UTC),
            verified_by=["Exa"],
            source_hint="TechCrunch",
            proof_hash="funding-1",
        ),
        SignalEvidence(
            slug="funding",
            source_url="https://press.dev/acme?token=secret",
            timestamp=datetime(2025, 1, 6, 12, 0, tzinfo=UTC),
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
