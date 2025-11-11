from pathlib import Path

from tools import verify_feedback_resolver

FIXTURE_DIR = Path("tests/fixtures/bundles/feedback_case/leads")


def test_feedback_resolver_promotes_entity(tmp_path: Path):
    normalized = tmp_path / "exa_seed.normalized.json"
    normalized.write_text((FIXTURE_DIR / "exa_seed.normalized.json").read_text(), encoding="utf-8")
    youcom = FIXTURE_DIR / "youcom_verified.json"
    tavily = FIXTURE_DIR / "tavily_verified.json"
    output = tmp_path / "exa_seed.feedback_resolved.json"

    summary = verify_feedback_resolver.apply_feedback(normalized, output, youcom, tavily)

    assert summary["feedback_applied"] == 1
    payload = verify_feedback_resolver.load_json(output)
    rows = payload["data"]
    updated = next(row for row in rows if row["id"] == "row_hotglue")
    assert updated["company_name"] == "Hotglue"
    assert updated["feedback_applied"] is True
    assert updated["feedback_domains"] == ["businesswire.com", "techcrunch.com", "venturebeat.com"]


def test_feedback_resolver_is_deterministic(tmp_path: Path):
    normalized = tmp_path / "exa_seed.normalized.json"
    normalized.write_text((FIXTURE_DIR / "exa_seed.normalized.json").read_text(), encoding="utf-8")
    youcom = FIXTURE_DIR / "youcom_verified.json"
    tavily = FIXTURE_DIR / "tavily_verified.json"
    output = tmp_path / "exa_seed.feedback_resolved.json"

    verify_feedback_resolver.apply_feedback(normalized, output, youcom, tavily)
    first_bytes = output.read_bytes()
    verify_feedback_resolver.apply_feedback(normalized, output, youcom, tavily)
    assert first_bytes == output.read_bytes()
