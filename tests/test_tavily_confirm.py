import json
from datetime import UTC, datetime
from pathlib import Path

from app.clients.tavily import TavilyRateLimitError
from app.models.lead import CompanyFunding
from pipelines.day1 import tavily_confirm


def make_lead(company: str, *, youcom_verified: bool = True) -> CompanyFunding:
    return CompanyFunding(
        company=company,
        funding_amount=15_000_000,
        funding_stage="Series B",
        funding_date=datetime(2025, 9, 2, tzinfo=UTC).date(),
        source_url="https://example.com/acme",
        discovered_at=datetime(2025, 11, 5, tzinfo=UTC),
        youcom_verified=youcom_verified,
        youcom_verified_at=datetime(2025, 11, 6, tzinfo=UTC),
        news_sources=["TechCrunch", "BusinessWire"],
        press_articles=["https://techcrunch.com/acme", "https://businesswire.com/acme"],
    )


class StubTavilyClient:
    def __init__(self, responses):
        self.responses = responses
        self.queries: list[str] = []

    def search(self, *, query: str, max_results: int, days_limit: int | None = None):
        self.queries.append(query)
        return self.responses.get(query, [])


def test_run_pipeline_confirms_leads(tmp_path: Path):
    lead = make_lead("Acme SaaS")
    input_path = tmp_path / "youcom_verified.json"
    input_path.write_text(json.dumps([lead.model_dump(mode="json")]), encoding="utf-8")

    query = tavily_confirm.build_query(lead)
    stub_client = StubTavilyClient(
        {
            query: [
                {
                    "url": "https://analystreports.com/acme-series-b",
                    "title": "Acme SaaS raises $15M Series B",
                    "snippet": "AnalystReports discusses Acme SaaS and its $15,000,000 Series B funding round.",
                },
                {
                    "url": "https://blog.example.com/acme-funding",
                    "title": "Blog: Acme SaaS secures Series B funding",
                    "snippet": "The Series B raise of $15M helps Acme SaaS expand.",
                },
            ]
        }
    )

    output_path = tmp_path / "tavily_confirmed.json"
    records = tavily_confirm.run_pipeline(
        input_path=input_path,
        output_path=output_path,
        min_confirmations=2,
        max_results=5,
        client=stub_client,
    )

    assert output_path.exists()
    assert stub_client.queries
    record = records[0]
    assert record.tavily_verified is True
    assert record.tavily_reason is None
    assert len(record.proof_links) == 2
    assert record.tavily_verified_at is not None


def test_insufficient_sources_sets_reason(tmp_path: Path):
    lead = make_lead("Beta Cloud")
    input_path = tmp_path / "youcom_verified.json"
    input_path.write_text(json.dumps([lead.model_dump(mode="json")]), encoding="utf-8")

    query = tavily_confirm.build_query(lead)
    stub_client = StubTavilyClient(
        {
            query: [
                {
                    "url": "https://mirror.example.com/beta-cloud",
                    "title": "Beta Cloud raises $15M Series B",
                    "snippet": "Duplicate coverage for Beta Cloud $15,000,000 Series B funding.",
                },
                {
                    "url": "https://mirror.example.com/beta-cloud-copy",
                    "title": "Beta Cloud raises $15M Series B (copy)",
                    "snippet": "Same domain duplicate mention.",
                },
            ]
        }
    )

    records = tavily_confirm.run_pipeline(
        input_path=input_path,
        output_path=tmp_path / "out.json",
        min_confirmations=2,
        max_results=5,
        client=stub_client,
    )

    record = records[0]
    assert record.tavily_verified is False
    assert record.tavily_reason == "insufficient_sources"
    assert record.proof_links == []


def test_discover_with_retries_handles_rate_limit():
    sleeps = []

    def fake_sleep(seconds: float):
        sleeps.append(seconds)

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def search(self, *, query: str, max_results: int, days_limit: int | None = None):
            self.calls += 1
            if self.calls == 1:
                raise TavilyRateLimitError()
            return [
                {
                    "url": "https://analysis.example.com/funding",
                    "title": "Example raises $8M Series A",
                    "snippet": "Discussion of Example raising $8,000,000 Series A funding.",
                }
            ]

    client = FlakyClient()
    results = tavily_confirm.discover_with_retries(
        client,
        query="Example",
        max_results=5,
        sleep=fake_sleep,
    )

    assert len(results) == 1
    assert sleeps
    assert client.calls == 2
