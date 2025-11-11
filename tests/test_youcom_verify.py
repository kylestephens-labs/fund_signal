import json
from datetime import UTC, datetime
from pathlib import Path

from app.clients.youcom import YoucomRateLimitError
from app.models.lead import CompanyFunding
from pipelines.day1 import youcom_verify


class StubYoucomClient:
    """Stub client returning predefined You.com responses."""

    def __init__(self, query_map):
        self.query_map = query_map
        self.calls: list[str] = []

    def search_news(self, *, query: str, limit: int, time_filter: str | None = None):
        self.calls.append(query)
        return self.query_map.get(query, [])


def make_lead(company: str) -> CompanyFunding:
    return CompanyFunding(
        company=company,
        funding_amount=12_000_000,
        funding_stage="Series A",
        funding_date=datetime(2025, 9, 2, tzinfo=UTC).date(),
        source_url="https://example.com/acme",
        discovered_at=datetime(2025, 11, 5, tzinfo=UTC),
    )


def make_article(url: str, publisher: str, title: str, snippet: str) -> dict:
    return {
        "url": url,
        "publisher": publisher,
        "title": title,
        "snippet": snippet,
    }


def test_run_pipeline_verifies_companies(tmp_path: Path):
    lead = make_lead("Acme SaaS")
    input_path = tmp_path / "exa_seed.json"
    input_path.write_text(json.dumps([lead.model_dump(mode="json")]), encoding="utf-8")

    query = youcom_verify.build_query(lead)
    stub_client = StubYoucomClient(
        {
            query: [
                make_article(
                    "https://techcrunch.com/acme-series-a",
                    "TechCrunch",
                    "Acme SaaS raises $12M Series A",
                    "Acme SaaS announced a $12 million Series A funding round.",
                ),
                make_article(
                    "https://www.businesswire.com/news/home/20250902005000/en/Acme-SaaS-Series-A",
                    "BusinessWire",
                    "BusinessWire: Acme SaaS Secures Series A",
                    "BusinessWire covers the $12,000,000 Series A for Acme SaaS.",
                ),
            ]
        }
    )

    output_path = tmp_path / "youcom_verified.json"
    records = youcom_verify.run_pipeline(
        input_path=input_path,
        output_path=output_path,
        min_articles=2,
        max_results=5,
        client=stub_client,
    )

    assert output_path.exists()
    assert stub_client.calls  # Query executed
    assert records[0].youcom_verified is True
    assert len(records[0].news_sources) == 2
    assert len(records[0].press_articles) == 2
    assert records[0].youcom_verified_at is not None


def test_verify_requires_distinct_publishers(tmp_path: Path):
    lead = make_lead("Beta Cloud")
    input_path = tmp_path / "exa_seed.json"
    input_path.write_text(json.dumps([lead.model_dump(mode="json")]), encoding="utf-8")

    query = youcom_verify.build_query(lead)
    stub_client = StubYoucomClient(
        {
            query: [
                make_article(
                    "https://newsroom.example.com/beta-cloud",
                    "Newsroom",
                    "Beta Cloud raises Series A",
                    "Beta Cloud secures $12M Series A funding.",
                ),
                make_article(
                    "https://newsroom.example.com/beta-cloud-duplicate",
                    "Newsroom",
                    "Beta Cloud raises Series A again",
                    "Duplicate coverage from same outlet.",
                ),
            ]
        }
    )

    records = youcom_verify.run_pipeline(
        input_path=input_path,
        output_path=tmp_path / "out.json",
        min_articles=2,
        max_results=5,
        client=stub_client,
    )

    assert records[0].youcom_verified is False
    assert records[0].news_sources == []
    assert records[0].press_articles == []


def test_discover_with_retries_handles_rate_limits():
    captured_sleep = []

    def fake_sleep(seconds: float):
        captured_sleep.append(seconds)

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def search_news(self, *, query: str, limit: int, time_filter: str | None = None):
            self.calls += 1
            if self.calls == 1:
                raise YoucomRateLimitError()
            return [
                {
                    "url": "https://techcrunch.com/example",
                    "publisher": "TechCrunch",
                    "title": "Example raises $10M Series A",
                    "snippet": "Example raises $10,000,000 Series A funding.",
                }
            ]

    client = FlakyClient()
    articles = youcom_verify.discover_with_retries(
        client,
        query="Example",
        limit=5,
        sleep=fake_sleep,
    )

    assert len(articles) == 1
    assert captured_sleep  # Backoff executed
    assert client.calls == 2
