import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.lead import CompanyFunding
from pipelines.day1 import confidence_scoring


def make_lead(
    *,
    exa: bool,
    youcom: bool,
    tavily: bool,
    company: str = "Acme SaaS",
) -> CompanyFunding:
    return CompanyFunding(
        company=company,
        funding_amount=10_000_000,
        funding_stage="Series A",
        funding_date=datetime(2025, 9, 2, tzinfo=timezone.utc).date(),
        source_url="https://example.com/acme",
        discovered_at=datetime(2025, 11, 5, tzinfo=timezone.utc),
        exa_found=exa,
        youcom_verified=youcom,
        youcom_verified_at=datetime(2025, 11, 6, tzinfo=timezone.utc) if youcom else None,
        tavily_verified=tavily,
        tavily_verified_at=datetime(2025, 11, 7, tzinfo=timezone.utc) if tavily else None,
        news_sources=["TechCrunch"] if youcom else [],
        press_articles=["https://techcrunch.com/acme"] if youcom else [],
        proof_links=["https://analystreports.com/acme"] if tavily else [],
    )


def test_run_pipeline_filters_confidence(tmp_path: Path):
    leads = [
        make_lead(exa=True, youcom=True, tavily=True, company="Verified Co"),
        make_lead(exa=True, youcom=True, tavily=False, company="Likely Co"),
        make_lead(exa=True, youcom=False, tavily=False, company="Exclude Co"),
    ]
    input_path = tmp_path / "tavily_confirmed.json"
    input_path.write_text(json.dumps([lead.model_dump(mode="json") for lead in leads]), encoding="utf-8")

    output_path = tmp_path / "day1_output.json"
    records = confidence_scoring.run_pipeline(input_path, output_path)

    assert output_path.exists()
    assert len(records) == 2  # VERIFIED and LIKELY only
    confidence_labels = {lead.company: lead.confidence for lead in records}
    assert confidence_labels["Verified Co"] == "VERIFIED"
    assert confidence_labels["Likely Co"] == "LIKELY"

    for record in records:
        assert record.verified_by
        assert record.last_checked_at is not None
        assert record.freshness_watermark


def test_enrich_lead_raises_when_no_sources():
    lead = make_lead(exa=False, youcom=False, tavily=False)
    with pytest.raises(confidence_scoring.ConfidenceError) as excinfo:
        confidence_scoring.enrich_lead(lead, timestamp=datetime.now(tz=timezone.utc))
    assert excinfo.value.code == "CONF_INPUT_ERR"


def test_build_watermark_uses_confidence():
    lead = make_lead(exa=True, youcom=True, tavily=True)
    lead.verified_by = ["Exa", "You.com", "Tavily"]
    lead.confidence = "VERIFIED"
    lead.last_checked_at = datetime(2025, 11, 8, tzinfo=timezone.utc)
    watermark = confidence_scoring.build_watermark(lead)
    assert "Verified by: Exa, You.com, Tavily" in watermark
    assert "Confidence: HIGH" in watermark
