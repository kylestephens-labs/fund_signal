import hashlib
import json
from pathlib import Path

import pytest

from pipelines.day1 import confidence_scoring_v2


def _write_unified(input_path: Path, payload: list[dict]) -> None:
    input_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_scoring_applies_heuristics(tmp_path: Path):
    input_path = tmp_path / "unified_verify.json"
    leads = [
        {
            "id": "lead_001",
            "company_name": "Acme AI",
            "normalized": {
                "stage": "Series A",
                "amount": {"value": 8, "unit": "M", "currency": "USD"},
            },
            "confirmations": {
                "youcom": [
                    {
                        "url": "https://techcrunch.com/acme-series-a",
                        "domain": "techcrunch.com",
                        "match": {"stage": True, "amount": True},
                    }
                ],
                "tavily": [
                    {
                        "url": "https://www.businesswire.com/news/home/20240101005000/en/",
                        "domain": "businesswire.com",
                        "match": {"stage": True, "amount": False},
                    }
                ],
            },
        }
    ]
    _write_unified(input_path, leads)

    output_path = tmp_path / "day1_scored.json"
    rules_path = Path("configs/verification_rules.v1.yaml")

    scored = confidence_scoring_v2.run_pipeline(input_path, rules_path, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ruleset_version"].startswith("v")
    assert payload["ruleset_sha256"]
    assert payload["leads"][0]["final_label"] == "VERIFIED"
    assert payload["leads"][0]["confidence_points"] >= 3
    assert payload["leads"][0]["verified_by"] == ["Exa", "You.com", "Tavily"]
    assert payload["leads"][0]["proof_links"] == [
        "https://techcrunch.com/acme-series-a",
        "https://www.businesswire.com/news/home/20240101005000/en/",
    ]

    assert scored[0].confidence_points == payload["leads"][0]["confidence_points"]


def test_rules_are_deterministic(tmp_path: Path):
    input_path = tmp_path / "unified_verify.json"
    leads = [
        {
            "id": "lead_002",
            "company_name": "Beta Robotics",
            "normalized": {"stage": "Seed", "amount": {"value": 3}},
            "confirmations": {
                "youcom": [
                    {
                        "url": "https://techcrunch.com/beta",
                        "domain": "techcrunch.com",
                        "match": {"amount": True},
                    }
                ],
                "tavily": [
                    {
                        "url": "https://www.businesswire.com/news/home/20240102005000/en/",
                        "domain": "businesswire.com",
                        "match": {"stage": True},
                    }
                ],
            },
        }
    ]
    _write_unified(input_path, leads)

    output_path = tmp_path / "day1_scored.json"
    rules_path = Path("configs/verification_rules.v1.yaml")

    confidence_scoring_v2.run_pipeline(input_path, rules_path, output_path)
    first_bytes = output_path.read_bytes()
    confidence_scoring_v2.run_pipeline(input_path, rules_path, output_path)
    second_bytes = output_path.read_bytes()

    assert first_bytes == second_bytes
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(second_bytes).hexdigest()


def test_rules_version_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    input_path = tmp_path / "unified_verify.json"
    leads = [
        {
            "id": "lead_003",
            "company_name": "Gamma Cloud",
            "normalized": {"stage": "Series B", "amount": {"value": 20}},
            "confirmations": {
                "youcom": [
                    {
                        "url": "https://techcrunch.com/gamma",
                        "domain": "techcrunch.com",
                        "match": {"stage": True},
                    }
                ],
                "tavily": [
                    {
                        "url": "https://www.businesswire.com/gamma",
                        "domain": "businesswire.com",
                        "match": {"amount": True},
                    }
                ],
            },
        }
    ]
    _write_unified(input_path, leads)

    output_path = tmp_path / "day1_scored.json"
    rules_path = Path("configs/verification_rules.v1.yaml")
    monkeypatch.setenv("RULES_VERSION_OVERRIDE", "v-test")

    confidence_scoring_v2.run_pipeline(input_path, rules_path, output_path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    expected_sha = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    assert payload["ruleset_version"] == "v-test"
    assert payload["ruleset_sha256"] == expected_sha


def test_timestamp_override_controls_scored_at(tmp_path: Path):
    input_path = tmp_path / "unified_verify.json"
    leads = [
        {
            "id": "lead_999",
            "company_name": "Theta AI",
            "normalized": {"stage": "Seed", "amount": {"value": 1.5}},
            "confirmations": {
                "youcom": [
                    {
                        "url": "https://techcrunch.com/theta",
                        "domain": "techcrunch.com",
                        "match": {"amount": True},
                    }
                ],
                "tavily": [],
            },
        }
    ]
    _write_unified(input_path, leads)

    output_path = tmp_path / "day1_scored.json"
    override = "2025-03-01T08:15:00Z"

    confidence_scoring_v2.run_pipeline(
        input_path,
        Path("configs/verification_rules.v1.yaml"),
        output_path,
        timestamp_override=override,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["scored_at"] == override


def test_timestamp_override_produces_stable_scoring_file(tmp_path: Path):
    input_path = tmp_path / "unified_verify.json"
    leads = [
        {
            "id": "lead-stable",
            "company_name": "Lambda AI",
            "normalized": {"stage": "Series A", "amount": {"value": 7}},
            "confirmations": {
                "youcom": [
                    {
                        "url": "https://techcrunch.com/lambda",
                        "domain": "techcrunch.com",
                        "match": {"stage": True},
                    }
                ],
                "tavily": [
                    {
                        "url": "https://wire.example/lambda",
                        "domain": "wire.example",
                        "match": {"amount": True},
                    }
                ],
            },
        }
    ]
    _write_unified(input_path, leads)

    output_path = tmp_path / "day1_scored.json"
    override = "2025-04-10T05:00:00Z"

    confidence_scoring_v2.run_pipeline(
        input_path,
        Path("configs/verification_rules.v1.yaml"),
        output_path,
        timestamp_override=override,
    )
    first_bytes = output_path.read_bytes()

    confidence_scoring_v2.run_pipeline(
        input_path,
        Path("configs/verification_rules.v1.yaml"),
        output_path,
        timestamp_override=override,
    )
    second_bytes = output_path.read_bytes()

    assert first_bytes == second_bytes
