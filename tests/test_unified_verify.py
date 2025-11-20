import gzip
import json
from pathlib import Path

import pytest

from pipelines.day1 import unified_verify

pytestmark = pytest.mark.slow


def _write_normalized_seed(path: Path, entries: list[dict]) -> None:
    payload = {
        "normalizer_version": "1.0.0",
        "items_total": len(entries),
        "items_parsed": len(entries),
        "items_skipped": 0,
        "coverage_by_field": {
            "company_name": len(entries),
            "funding_stage": len(entries),
            "amount": len(entries),
            "announced_date": len(entries),
        },
        "data": entries,
        "skipped": [],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_fixture(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record))
            handle.write("\n")


@pytest.fixture(autouse=True)
def _force_fixture_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUND_SIGNAL_MODE", "fixture")
    monkeypatch.setenv("FUND_SIGNAL_SOURCE", "local")
    monkeypatch.delenv("FUND_SIGNAL_FIXTURE_DIR", raising=False)


def test_unified_verify_merges_sources(tmp_path: Path):
    seed_path = tmp_path / "exa_seed.normalized.json"
    youcom_path = tmp_path / "youcom.jsonl.gz"
    tavily_path = tmp_path / "tavily.jsonl.gz"
    output_path = tmp_path / "unified_verify.json"

    _write_normalized_seed(
        seed_path,
        [
            {
                "company_name": "Acme AI",
                "funding_stage": "Series A",
                "amount": {"value": 8, "unit": "M", "currency": "USD"},
                "announced_date": "2025-10-15",
                "source_url": "https://exa.example/acme",
                "raw_title": "Acme AI raises $8M Series A",
            }
        ],
    )

    _write_fixture(
        youcom_path,
        [
            {
                "slug": "acme-ai",
                "data": [
                    {
                        "url": "https://techcrunch.com/acme-ai-series-a",
                        "title": "Acme AI raises $8M Series A",
                        "snippet": "Acme AI raises $8 million in Series A funding.",
                        "page_age": "2025-10-15T00:00:00Z",
                    }
                ],
            }
        ],
    )
    _write_fixture(
        tavily_path,
        [
            {
                "slug": "acme-ai",
                "data": [
                    {
                        "url": "https://www.businesswire.com/news/home/20250101005000/en/Acme-AI-Series-A",
                        "title": "BusinessWire: Acme AI completes Series A",
                        "content": "Acme AI secured $8 million Series A funding round.",
                    },
                    {
                        "url": "https://techcrunch.com/acme-ai-series-a",
                        "title": "TechCrunch duplicate",
                        "content": "Acme AI raises $8M Series A per TechCrunch.",
                    },
                ],
            }
        ],
    )

    payload = unified_verify.run_pipeline(
        seed_path=seed_path,
        youcom_path=youcom_path,
        tavily_path=tavily_path,
        output_path=output_path,
        youcom_limit=5,
        tavily_limit=5,
    )

    assert output_path.exists()
    assert payload["metrics"]["youcom_hits"] == 1
    assert payload["metrics"]["tavily_hits"] == 2
    lead = payload["leads"][0]
    assert lead["company_name"] == "Acme AI"
    assert lead["verified_by"] == ["Exa", "You.com", "Tavily"]
    assert lead["unique_domains_total"] == 2
    assert lead["unique_domains_by_source"] == {"youcom": 1, "tavily": 2}
    assert lead["articles_all"] == [
        {"url": "https://techcrunch.com/acme-ai-series-a", "domain": "techcrunch.com"},
        {
            "url": "https://www.businesswire.com/news/home/20250101005000/en/Acme-AI-Series-A",
            "domain": "businesswire.com",
        },
    ]


def test_verified_by_requires_confirming_articles(tmp_path: Path):
    seed_path = tmp_path / "exa_seed.normalized.json"
    youcom_path = tmp_path / "youcom.jsonl.gz"
    tavily_path = tmp_path / "tavily.jsonl.gz"
    output_path = tmp_path / "unified_verify.json"

    _write_normalized_seed(
        seed_path,
        [
            {
                "company_name": "Beta Cloud",
                "funding_stage": "Series B",
                "amount": {"value": 12, "unit": "M", "currency": "USD"},
                "source_url": "https://exa.example/beta",
            }
        ],
    )

    _write_fixture(
        youcom_path,
        [
            {
                "slug": "beta-cloud",
                "data": [
                    {
                        "url": "https://newsroom.example.com/beta-cloud",
                        "title": "Beta Cloud hires new CFO",
                        "snippet": "No funding info here.",
                    }
                ],
            }
        ],
    )
    _write_fixture(
        tavily_path,
        [
            {
                "slug": "beta-cloud",
                "data": [
                    {
                        "url": "https://www.businesswire.com/news/home/20250102005000/en/Beta-Cloud-Series-B",
                        "title": "Beta Cloud secures Series B",
                        "content": "Beta Cloud raises $12 million Series B funding.",
                    }
                ],
            }
        ],
    )

    payload = unified_verify.run_pipeline(
        seed_path=seed_path,
        youcom_path=youcom_path,
        tavily_path=tavily_path,
        output_path=output_path,
        youcom_limit=5,
        tavily_limit=5,
    )

    lead = payload["leads"][0]
    assert lead["verified_by"] == ["Exa", "Tavily"]
    assert lead["unique_domains_by_source"] == {"youcom": 0, "tavily": 1}
    assert lead["confirmations"]["youcom"][0]["match"] == {"stage": False, "amount": False}
    assert lead["confirmations"]["tavily"][0]["match"] == {"stage": True, "amount": True}


def test_missing_fixture_inputs_are_tolerated(tmp_path: Path):
    seed_path = tmp_path / "exa_seed.normalized.json"
    youcom_path = tmp_path / "youcom.jsonl.gz"
    tavily_path = tmp_path / "missing_tavily.jsonl.gz"
    output_path = tmp_path / "unified_verify.json"

    _write_normalized_seed(
        seed_path,
        [
            {
                "company_name": "Gamma Data",
                "funding_stage": "Seed",
                "amount": {"value": 2.5, "unit": "M", "currency": "USD"},
                "source_url": "https://exa.example/gamma",
            }
        ],
    )

    _write_fixture(
        youcom_path,
        [
            {
                "slug": "gamma-data",
                "data": [
                    {
                        "url": "https://techcrunch.com/gamma-data-seed",
                        "title": "Gamma Data raises $2.5M seed round",
                        "snippet": "Gamma Data raises $2.5 million seed funding.",
                    }
                ],
            }
        ],
    )

    payload = unified_verify.run_pipeline(
        seed_path=seed_path,
        youcom_path=youcom_path,
        tavily_path=tavily_path,
        output_path=output_path,
        youcom_limit=5,
        tavily_limit=5,
    )

    lead = payload["leads"][0]
    assert lead["verified_by"] == ["Exa", "You.com"]
    assert lead["confirmations"]["tavily"] == []
    assert payload["metrics"]["tavily_hits"] == 0


def test_timestamp_override_sets_generated_at(tmp_path: Path):
    seed_path = tmp_path / "exa_seed.normalized.json"
    youcom_path = tmp_path / "youcom.jsonl.gz"
    tavily_path = tmp_path / "tavily.jsonl.gz"
    output_path = tmp_path / "unified_verify.json"

    _write_normalized_seed(
        seed_path,
        [
            {
                "company_name": "Delta AI",
                "funding_stage": "Series A",
                "amount": {"value": 10, "unit": "M", "currency": "USD"},
                "source_url": "https://exa.example/delta",
            }
        ],
    )

    _write_fixture(
        youcom_path,
        [
            {
                "slug": "delta-ai",
                "data": [
                    {
                        "url": "https://press.example/delta",
                        "title": "Delta AI raises $10M Series A",
                        "snippet": "Delta AI raises.",
                    }
                ],
            }
        ],
    )
    _write_fixture(
        tavily_path,
        [
            {
                "slug": "delta-ai",
                "data": [
                    {
                        "url": "https://wire.example/delta",
                        "title": "Delta AI raises $10M",
                        "content": "Delta AI raises.",
                    }
                ],
            }
        ],
    )

    override = "2025-02-01T12:30:00Z"
    payload = unified_verify.run_pipeline(
        seed_path=seed_path,
        youcom_path=youcom_path,
        tavily_path=tavily_path,
        output_path=output_path,
        youcom_limit=3,
        tavily_limit=3,
        timestamp_override=override,
    )

    assert payload["generated_at"] == override


def test_timestamp_override_produces_stable_file(tmp_path: Path):
    seed_path = tmp_path / "exa_seed.normalized.json"
    youcom_path = tmp_path / "youcom.jsonl.gz"
    tavily_path = tmp_path / "tavily.jsonl.gz"
    output_path = tmp_path / "unified_verify.json"

    _write_normalized_seed(
        seed_path,
        [
            {
                "company_name": "Epsilon AI",
                "funding_stage": "Seed",
                "amount": {"value": 5, "unit": "M", "currency": "USD"},
                "source_url": "https://exa.example/epsilon",
            }
        ],
    )

    _write_fixture(
        youcom_path,
        [
            {
                "slug": "epsilon-ai",
                "data": [
                    {
                        "url": "https://press.example/epsilon",
                        "title": "Epsilon raises $5M",
                        "snippet": "Seed funding.",
                    }
                ],
            }
        ],
    )
    _write_fixture(
        tavily_path,
        [
            {
                "slug": "epsilon-ai",
                "data": [
                    {
                        "url": "https://wire.example/epsilon",
                        "title": "Epsilon funding",
                        "content": "Seed funding.",
                    }
                ],
            }
        ],
    )

    override = "2025-01-15T00:00:00Z"
    unified_verify.run_pipeline(
        seed_path=seed_path,
        youcom_path=youcom_path,
        tavily_path=tavily_path,
        output_path=output_path,
        youcom_limit=2,
        tavily_limit=2,
        timestamp_override=override,
    )
    first_bytes = output_path.read_bytes()

    unified_verify.run_pipeline(
        seed_path=seed_path,
        youcom_path=youcom_path,
        tavily_path=tavily_path,
        output_path=output_path,
        youcom_limit=2,
        tavily_limit=2,
        timestamp_override=override,
    )
    second_bytes = output_path.read_bytes()

    assert first_bytes == second_bytes
