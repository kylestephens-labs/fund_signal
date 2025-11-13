import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.models.company import CompanyProfile
from app.services.scoring.chatgpt_engine import (
    DEFAULT_SOURCE_MAP,
    ChatGPTScoringEngine,
    ScoringContext,
    ScoringValidationError,
)
from app.services.scoring.proof_links import ProofLinkHydrator

REGRESSION_FIXTURE_ENV = "SCORING_REGRESSION_FIXTURE"
DEFAULT_REGRESSION_FIXTURE = Path("tests/fixtures/scoring/regression_companies.json")


@dataclass(frozen=True)
class RegressionPersona:
    persona_id: str
    profile: CompanyProfile
    min_score: int
    max_score: int
    expected_rank: int


@dataclass(frozen=True)
class RegressionFixture:
    path: Path
    version: str
    personas: list[RegressionPersona]

    @property
    def context(self) -> str:
        return f"[fixture={self.path}] [version={self.version}]"


class StubOpenAIClient:
    """Deterministic stub for the OpenAI client."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0

    def generate(self, **_: str) -> str:
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def _sample_company(**overrides):
    payload = {
        "company_id": uuid4(),
        "name": "Acme SaaS",
        "funding_amount": "$12M",
        "funding_stage": "Series A",
        "days_since_funding": 75,
        "employee_count": 42,
        "job_postings": 6,
        "tech_stack": ["Salesforce", "HubSpot"],
        "buying_signals": ["https://techcrunch.com/acme"],
        "verified_sources": ["Exa", "Tavily"],
    }
    payload.update(overrides)
    return CompanyProfile(**payload)


def _load_regression_fixture() -> RegressionFixture:
    fixture_path = Path(os.getenv(REGRESSION_FIXTURE_ENV, DEFAULT_REGRESSION_FIXTURE))
    if not fixture_path.exists():
        pytest.fail(
            f"Regression fixture not found at {fixture_path}. "
            f"Override via {REGRESSION_FIXTURE_ENV} or restore the default file."
        )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    version = data.get("regression_version", "unknown")
    personas = [_parse_persona(raw, fixture_path) for raw in data.get("companies", [])]
    return RegressionFixture(path=fixture_path, version=version, personas=personas)


def _parse_persona(payload: dict[str, Any], fixture_path: Path) -> RegressionPersona:
    try:
        persona = RegressionPersona(
            persona_id=payload["id"],
            profile=CompanyProfile(**payload["profile"]),
            min_score=payload["min_score"],
            max_score=payload["max_score"],
            expected_rank=payload["expected_rank"],
        )
    except KeyError as exc:  # pragma: no cover - defensive field guard
        pytest.fail(f"[fixture={fixture_path}] missing regression field: {exc}")  # noqa: PT009
    if persona.min_score > persona.max_score:
        pytest.fail(
            f"[fixture={fixture_path}] persona {persona.persona_id} has min_score > max_score "
            f"({persona.min_score} > {persona.max_score})"
        )
    return persona


def test_fixture_rubric_scores_company_without_openai():
    engine = ChatGPTScoringEngine(
        context=ScoringContext(
            mode="fixture",
            system_prompt="system",
            model="test-model",
            temperature=0.1,
        ),
        proof_hydrator=ProofLinkHydrator(default_sources={}),
    )
    result = engine.score_company(_sample_company(), scoring_run_id="run-1")

    assert result.scoring_model == "fixture-rubric"
    assert result.score == sum(item.points for item in result.breakdown)
    assert result.recommended_approach
    assert result.pitch_angle


def test_online_mode_uses_cache_when_not_forced():
    response_body = json.dumps(
        {
            "company_id": "123",
            "score": 88,
            "breakdown": [
                {
                    "reason": "Funding 75 days ago",
                    "points": 28,
                    "source_url": "https://techcrunch.com/acme",
                    "verified_by": ["Exa"],
                },
                {
                    "reason": "5+ sales roles open",
                    "points": 60,
                    "source_url": "https://greenhouse.io/acme",
                    "verified_by": ["You.com"],
                },
            ],
            "recommended_approach": "Contact the founder via LinkedIn.",
            "pitch_angle": "Help Acme scale outbound.",
        }
    )
    stub_client = StubOpenAIClient([response_body])
    engine = ChatGPTScoringEngine(
        client=stub_client,
        context=ScoringContext(
            mode="online",
            system_prompt="system",
            model="gpt-test",
            temperature=0.1,
        ),
    )

    company = _sample_company()
    first = engine.score_company(company, scoring_run_id="run-cache")
    second = engine.score_company(company, scoring_run_id="run-cache")

    assert first.score == second.score == 88
    assert stub_client.calls == 1


def test_forced_run_invokes_provider_again():
    response_body = json.dumps(
        {
            "company_id": "123",
            "score": 70,
            "breakdown": [
                {
                    "reason": "Funding recency",
                    "points": 30,
                    "source_url": "https://techcrunch.com/acme",
                    "verified_by": ["Exa"],
                },
                {
                    "reason": "Hiring velocity",
                    "points": 40,
                    "source_url": "https://greenhouse.io/acme",
                    "verified_by": ["You.com"],
                },
            ],
            "recommended_approach": "Reach out to the VP of Sales.",
            "pitch_angle": "Help them staff outbound.",
        }
    )
    stub_client = StubOpenAIClient([response_body])
    engine = ChatGPTScoringEngine(
        client=stub_client,
        context=ScoringContext(
            mode="online",
            system_prompt="system",
            model="gpt-test",
            temperature=0.1,
        ),
    )

    company = _sample_company()
    engine.score_company(company, scoring_run_id="run-force", force=True)
    engine.score_company(company, scoring_run_id="run-force", force=True)

    assert stub_client.calls == 2


def test_invalid_json_from_openai_raises_validation_error():
    stub_client = StubOpenAIClient(["not-json"])
    engine = ChatGPTScoringEngine(
        client=stub_client,
        context=ScoringContext(
            mode="online",
            system_prompt="system",
            model="gpt-test",
            temperature=0.1,
        ),
    )

    with pytest.raises(ScoringValidationError) as excinfo:
        engine.score_company(_sample_company(), scoring_run_id="run-error", force=True)

    assert excinfo.value.code == "502_OPENAI_UPSTREAM"


def test_breakdown_adjusts_to_declared_score():
    response_body = json.dumps(
        {
            "company_id": "123",
            "score": 50,
            "breakdown": [
                {
                    "reason": "Funding recency",
                    "points": 30,
                    "source_url": "https://techcrunch.com/acme",
                    "verified_by": ["Exa"],
                },
                {
                    "reason": "Hiring velocity",
                    "points": 10,
                    "source_url": "https://greenhouse.io/acme",
                    "verified_by": ["You.com"],
                },
            ],
            "recommended_approach": "Email the GTM lead.",
            "pitch_angle": "Help them staff outbound.",
        }
    )
    stub_client = StubOpenAIClient([response_body])
    engine = ChatGPTScoringEngine(
        client=stub_client,
        context=ScoringContext(
            mode="online",
            system_prompt="system",
            model="gpt-test",
            temperature=0.1,
        ),
    )

    company = _sample_company()
    result = engine.score_company(company, scoring_run_id="run-adjust", force=True)

    assert result.score == 50
    assert sum(item.points for item in result.breakdown) == 50


def test_fixture_rubric_includes_multiple_proofs_from_buying_signals():
    engine = ChatGPTScoringEngine(
        context=ScoringContext(
            mode="fixture",
            system_prompt="system",
            model="test-model",
            temperature=0.1,
        ),
        proof_hydrator=ProofLinkHydrator(default_sources={}),
    )
    company = _sample_company(
        buying_signals=[
            "http://signals.dev/one?token=abc",
            "https://signals.dev/two",
        ]
    )

    score = engine.score_company(company, scoring_run_id="multi-proof")

    signals_breakdown = next(item for item in score.breakdown if "signals" in item.reason.lower())
    assert len(signals_breakdown.proofs) == 2
    assert [str(proof.source_url) for proof in signals_breakdown.proofs] == [
        "https://signals.dev/one",
        "https://signals.dev/two",
    ]


def test_fetch_scores_returns_runs():
    engine = ChatGPTScoringEngine(
        context=ScoringContext(
            mode="fixture",
            system_prompt="system",
            model="test-model",
            temperature=0.1,
        )
    )
    company = _sample_company()
    engine.score_company(company, scoring_run_id="run-1")
    engine.score_company(company, scoring_run_id="run-2", force=True)

    all_scores = engine.fetch_scores(str(company.company_id))
    run_1 = engine.fetch_scores(str(company.company_id), scoring_run_id="run-1")

    assert len(all_scores) == 2
    assert len(run_1) == 1
    assert run_1[0].scoring_run_id == "run-1"


def test_fixture_regression_scores_align_with_intuition():
    fixture = _load_regression_fixture()
    context = fixture.context
    assert len(fixture.personas) >= 3, f"{context} expected at least three regression personas."

    engine = ChatGPTScoringEngine(
        context=ScoringContext(
            mode="fixture",
            system_prompt="regression-system",
            model="fixture-rubric",
            temperature=0.0,
        ),
        proof_hydrator=ProofLinkHydrator(default_sources=DEFAULT_SOURCE_MAP),
    )

    scored: list[tuple[str, int]] = []
    for persona in fixture.personas:
        result = engine.score_company(persona.profile, scoring_run_id=f"regression-{persona.persona_id}", force=True)
        assert persona.min_score <= result.score <= persona.max_score, (
            f"{context} {persona.persona_id} expected score between {persona.min_score}-{persona.max_score}, "
            f"got {result.score}"
        )
        scored.append((persona.persona_id, result.score))

    expected_order = [persona.persona_id for persona in sorted(fixture.personas, key=lambda p: p.expected_rank)]
    actual_order = [persona_id for persona_id, _ in sorted(scored, key=lambda row: row[1], reverse=True)]
    assert actual_order == expected_order, f"{context} unexpected ranking {actual_order}; expected {expected_order}"
