import json
from pathlib import Path

import pytest

from pipelines.day1 import confidence_scoring
from tests.utils import create_canonical_bundle


def _build_bundle(tmp_path: Path) -> Path:
    youcom = [
        {
            "company": "Acme AI",
            "press_articles": ["https://you.com/news/acme?api_key=secret&view=1"],
            "news_sources": ["TechCrunch"],
            "youcom_verified": True,
        },
        {
            "company": "Beta Robotics",
            "press_articles": ["https://you.com/news/beta"],
            "news_sources": ["Forbes"],
            "youcom_verified": True,
        },
    ]
    tavily = [
        {
            "company": "Acme AI",
            "proof_links": ["https://tavily.com/posts/acme"],
            "tavily_verified": True,
        },
    ]
    exa = [
        {"company": "Acme AI", "source_url": "https://exa.ai/records/acme"},
        {"company": "Beta Robotics", "source_url": "https://exa.ai/records/beta"},
    ]
    return create_canonical_bundle(
        tmp_path,
        youcom=youcom,
        tavily=tavily,
        exa=exa,
        captured_at="2025-11-07T03:00:00Z",
    )


def test_run_pipeline_builds_expected_output(tmp_path: Path):
    bundle_root = _build_bundle(tmp_path)
    output_path = tmp_path / "day1_output.json"

    records = confidence_scoring.run_pipeline(bundle_root, output_path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    leads = payload["leads"]

    assert payload["bundle_id"] == "bundle-test"
    assert len(records) == len(leads) == 2
    assert [lead["company"] for lead in leads] == ["Acme AI", "Beta Robotics"]

    acme = leads[0]
    assert acme["confidence"] == "VERIFIED"
    assert acme["verified_by"] == ["exa", "youcom", "tavily"]
    assert acme["proof_links"][0] == "https://you.com/news/acme?view=1"
    assert acme["captured_at"] == "2025-11-07T03:00:00Z"

    beta = leads[1]
    assert beta["confidence"] == "LIKELY"
    assert beta["verified_by"] == ["exa", "youcom"]
    assert beta["proof_links"] == [
        "https://you.com/news/beta",
        "https://exa.ai/records/beta",
    ]


def test_missing_artifact_raises(tmp_path: Path):
    bundle_root = _build_bundle(tmp_path)
    (bundle_root / "leads" / "youcom_verified.json").unlink()
    with pytest.raises(confidence_scoring.ConfidenceError) as excinfo:
        confidence_scoring.run_pipeline(bundle_root, tmp_path / "out.json")
    assert excinfo.value.code == "E_CANONICAL_INPUT_MISSING"


def test_schema_validation_error(tmp_path: Path):
    bundle_root = _build_bundle(tmp_path)
    (bundle_root / "leads" / "youcom_verified.json").write_text("{}", encoding="utf-8")
    with pytest.raises(confidence_scoring.ConfidenceError) as excinfo:
        confidence_scoring.run_pipeline(bundle_root, tmp_path / "out.json")
    assert excinfo.value.code == "E_SCHEMA_INVALID"
