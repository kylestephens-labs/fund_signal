import json
from pathlib import Path

from tools.normalize_exa_seed import SeedNormalizer, normalize_file, normalize_records


def test_seed_normalizer_extracts_core_fields(tmp_path):
    record = {
        "company": "SaaSWeekly: Acme raises $8M Series A to scale GTM",
        "funding_amount": 8_000_000,
        "funding_stage": "Series A",
        "funding_date": "2025-09-15",
        "source_url": "https://example.com/acme-raises",
    }
    normalizer = SeedNormalizer()
    normalized, error, meta = normalizer.normalize(record)
    assert error is None
    assert normalized
    assert normalized.company_name == "Acme"
    assert normalized.funding_stage == "Series A"
    assert normalized.amount.value == 8
    assert normalized.amount.unit == "M"
    assert normalized.announced_date.isoformat() == "2025-09-15"
    assert meta["extraction_method"] in {
        "regex",
        "publisher_split",
        "delimiter_regex",
        "delimiter_plain",
    }


def test_normalize_file_writes_payload(tmp_path):
    input_path = tmp_path / "exa_seed.jsonl"
    input_path.write_text(
        '{"company":"FintechTimes: Zeal secures $12M Series B","funding_amount":12000000,"funding_stage":"Series B","source_url":"https://fintechtimes.com/zeal"}\n',
        encoding="utf-8",
    )
    output_path = tmp_path / "normalized.json"
    payload = normalize_file(input_path, output_path)

    assert output_path.exists()
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["normalizer_version"] == "2.0.0"
    assert written["ruleset_version"]
    assert written["ruleset_sha256"]
    assert len(written["data"]) == 1
    assert written["data"][0]["company_name"] == "Zeal"
    assert written["data"][0]["extraction_method"]
    assert written["metrics"]["final_accepted"] == 1
    assert payload["items_total"] == 1


def test_fixture_sample_normalizes_successfully(tmp_path):
    fixture_src = Path("fixtures/sample/bundle-sample/raw/exa_seed.json")
    fixture_dst = tmp_path / "fixture.json"
    fixture_dst.write_text(fixture_src.read_text(encoding="utf-8"), encoding="utf-8")
    output_path = tmp_path / "fixture.normalized.json"

    payload = normalize_file(fixture_dst, output_path)
    assert payload["items_total"] == 1
    assert payload["items_parsed"] == 1
    normalized = payload["data"][0]
    assert normalized["company_name"] == "Acme SaaS"
    assert normalized["funding_stage"] == "Series A"


def test_normalize_records_reports_skip_reasons():
    records = (
        {
            "company": "Weekly SaaS: Apex raises $5M Seed round",
        },
        {
            "company": "No Amount Co.",
            "source_url": "https://example.com/no-amount",
            "snippet": "No raise was disclosed.",
        },
    )

    payload = normalize_records(records)
    assert payload["items_total"] == 2
    assert payload["items_skipped"] == 2
    reasons = {entry["skip_reason"] for entry in payload["skipped"]}
    assert {"MISSING_SOURCE_URL", "INVALID_AMOUNT"} <= reasons


def test_publisher_split_detection():
    record = {
        "company": "The SaaS News | Nimbus raises $10M Series A",
        "funding_amount": 10_000_000,
        "source_url": "https://www.thesaasnews.com/news/nimbus-raises-10m",
    }
    normalizer = SeedNormalizer()
    normalized, error, meta = normalizer.normalize(record)
    assert error is None
    assert normalized.company_name == "Nimbus"
    assert meta["publisher_flagged"] is True
    assert meta["publisher_split_used"] is True
