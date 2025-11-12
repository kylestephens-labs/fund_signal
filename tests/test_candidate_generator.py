import json
from pathlib import Path

from tools import candidate_generator


def _write_input(tmp_path: Path, rows: list[dict]) -> Path:
    input_path = tmp_path / "exa_seed.json"
    input_path.write_text(json.dumps(rows), encoding="utf-8")
    return input_path


def test_candidate_generator_emits_candidates(tmp_path: Path):
    rows = [
        {
            "id": "row_001",
            "title": "The SaaS News | Appy.ai raises $10M Series A",
            "snippet": "Appy.ai raises $10M Series A.",
            "url": "https://www.thesaasnews.com/news/appy.ai-raises-10m",
        }
    ]
    input_path = _write_input(tmp_path, rows)
    output_path = tmp_path / "candidates.json"

    payload = candidate_generator.generate_candidates(
        input_path=input_path,
        output_path=output_path,
        rules_path=Path("configs/normalizer_rules.v1.yaml"),
    )

    data = payload["data"]
    assert len(data) == 1
    row = data[0]
    assert row["features"]["publisher_flagged"] is True
    assert row["features"]["url_slug_used"] is True
    assert "Appy.ai" in row["candidates"]
    assert row["extraction_methods"]["Appy.ai"] == "url_slug"
    candidate_meta = row.get("candidate_features") or {}
    assert "Appy.ai" in candidate_meta
    assert "possessive_plural_repaired" in candidate_meta["Appy.ai"]
    assert payload["metrics"]["avg_candidates_per_item"] >= 1


def test_candidate_generator_is_deterministic(tmp_path: Path):
    rows = [
        {
            "title": "Daily Digest | Nimbus secures $12M Series B",
            "url": "https://www.dailydigest.com/news/nimbus-secures-12m",
        }
    ]
    input_path = _write_input(tmp_path, rows)
    output_path = tmp_path / "deterministic.json"

    candidate_generator.generate_candidates(
        input_path=input_path,
        output_path=output_path,
        rules_path=Path("configs/normalizer_rules.v1.yaml"),
    )
    first = output_path.read_bytes()

    candidate_generator.generate_candidates(
        input_path=input_path,
        output_path=output_path,
        rules_path=Path("configs/normalizer_rules.v1.yaml"),
    )
    second = output_path.read_bytes()

    assert first == second


def test_slug_parser_trims_verbs(tmp_path: Path):
    rows = [
        {
            "title": "Announcement | Hotglue raises $5M Seed",
            "url": "https://example.com/news/hotglue-raises-5m",
        }
    ]
    input_path = _write_input(tmp_path, rows)
    output_path = tmp_path / "slug.json"

    payload = candidate_generator.generate_candidates(
        input_path=input_path,
        output_path=output_path,
        rules_path=Path("configs/normalizer_rules.v1.yaml"),
    )

    row = payload["data"][0]
    assert any(candidate.startswith("Hotglue") for candidate in row["candidates"])


def test_candidate_generator_marks_possessive_repairs(tmp_path: Path):
    rows = [
        {
            "title": "Glassflow's",
            "url": "",
        }
    ]
    input_path = _write_input(tmp_path, rows)
    output_path = tmp_path / "possessive.json"

    payload = candidate_generator.generate_candidates(
        input_path=input_path,
        output_path=output_path,
        rules_path=Path("configs/normalizer_rules.v1.yaml"),
    )

    row = payload["data"][0]
    meta = row["candidate_features"].get("Glassflow")
    assert meta is not None
    assert meta["possessive_plural_repaired"] is True
