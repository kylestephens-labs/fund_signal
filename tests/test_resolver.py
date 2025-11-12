import json
from pathlib import Path

from tools import resolve_company_name
from tools.resolver_rules import load_rules


def _write_candidates(tmp_path: Path, rows: list[dict]) -> Path:
    payload = {"data": rows, "items_total": len(rows)}
    input_path = tmp_path / "candidates.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    return input_path


def test_resolver_selects_best_candidate(tmp_path: Path):
    rows = [
        {
            "id": "row_001",
            "raw_title": "The SaaS News | Appy.ai raises $10M Series A",
            "source_url": "https://www.thesaasnews.com/news/appy.ai-raises-10m",
            "candidates": ["Appy.ai", "Appy", "Seed Round"],
            "funding_stage": "Series A",
            "funding_amount": 10_000_000,
            "funding_currency": "USD",
            "announced_date": "2025-10-01",
        }
    ]
    input_path = _write_candidates(tmp_path, rows)
    output_path = tmp_path / "resolved.json"
    rules = load_rules(Path("configs/resolver_rules.v1.1.yaml"))

    payload = resolve_company_name.resolve_company_name(
        input_path=input_path,
        output_path=output_path,
        rules=rules,
    )

    assert payload["items_resolved"] == 1
    row = payload["data"][0]
    assert row["company_name"] == "Appy.ai"
    assert row["resolution"]["chosen_idx"] == 0
    assert row["resolver_ruleset_version"] == rules.version
    flags = row["resolution"].get("feature_flags")
    assert isinstance(flags, list)
    assert any(entry["candidate"] == "Appy.ai" for entry in flags)


def test_resolver_tie_breakers_apply_in_order(tmp_path: Path):
    rows = [
        {
            "id": "row_002",
            "raw_title": "Digest: Alpha collaborates with Beta",
            "source_url": "https://example.com/companies",
            "candidates": ["Alpha Labs", "Beta Labs"],
            "funding_stage": "Seed",
            "funding_amount": 2_000_000,
            "funding_currency": "USD",
        }
    ]
    input_path = _write_candidates(tmp_path, rows)
    output_path = tmp_path / "resolved.json"
    rules = load_rules(Path("configs/resolver_rules.v1.1.yaml"))

    payload = resolve_company_name.resolve_company_name(
        input_path=input_path,
        output_path=output_path,
        rules=rules,
    )

    row = payload["data"][0]
    assert row["company_name"] in {"Alpha Labs", "Beta Labs"}
    # lexicographic_ci should choose "Alpha Labs" if scores tie
    assert row["company_name"] == "Alpha Labs"


def test_resolver_skips_empty_candidates(tmp_path: Path):
    rows = [
        {
            "id": "row_003",
            "raw_title": "Empty",
            "source_url": "https://example.com",
            "candidates": [],
            "funding_stage": "Seed",
            "funding_amount": 1_000_000,
            "funding_currency": "USD",
        }
    ]
    input_path = _write_candidates(tmp_path, rows)
    output_path = tmp_path / "resolved.json"
    rules = load_rules(Path("configs/resolver_rules.v1.1.yaml"))

    payload = resolve_company_name.resolve_company_name(
        input_path=input_path,
        output_path=output_path,
        rules=rules,
    )

    assert payload["items_skipped"] == 1
    assert payload["skipped"][0]["id"] == "row_003"


def test_resolver_emits_locale_feature_flags(tmp_path: Path):
    rows = [
        {
            "id": "row_locale",
            "raw_title": "The SaaS News: Glassflow erhält Millionen Dollar Seed",
            "source_url": "https://example.com/news/glassflow-erhaelt-millionen",
            "candidates": ["The SaaS News", "Glassflow"],
            "candidate_features": {
                "Glassflow": {"possessive_plural_repaired": False},
                "The SaaS News": {"possessive_plural_repaired": False},
            },
            "funding_stage": "Seed",
            "funding_amount": 5_000_000,
            "funding_currency": "USD",
        }
    ]
    input_path = _write_candidates(tmp_path, rows)
    output_path = tmp_path / "resolved.json"
    rules = load_rules(Path("configs/resolver_rules.v1.1.yaml"))

    payload = resolve_company_name.resolve_company_name(
        input_path=input_path,
        output_path=output_path,
        rules=rules,
    )

    row = payload["data"][0]
    flags = {entry["candidate"]: entry["signals"] for entry in row["resolution"]["feature_flags"]}
    assert flags["The SaaS News"]["has_publisher_prefix"] is True
    assert flags["Glassflow"]["locale_verb_hit"] is True
    assert flags["Glassflow"]["close_to_slug_head"] is True
