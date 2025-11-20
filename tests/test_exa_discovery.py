import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.clients.exa import ExaRateLimitError
from pipelines.day1 import exa_discovery


class StubExaClient:
    """Simple stub that returns predefined results."""

    def __init__(self, results):
        self._results = results
        self.calls = 0

    def search_recent_funding(self, **_: int):
        self.calls += 1
        return self._results


def make_result(idx: int, published: datetime) -> dict:
    company = f"Acme SaaS {idx}"
    amount_text = "$12M"
    title = f"{company} raises {amount_text} Series A to expand AI tooling"
    return {
        "title": title,
        "summary": title,
        "text": f"{company} announced a {amount_text} Series A funding round on {published.date().isoformat()}.",
        "url": f"https://example.com/funding/{idx}",
        "publishedDate": published.isoformat().replace("+00:00", "Z"),
    }


def test_run_pipeline_persists_normalized_records(tmp_path: Path):
    published = datetime.now(tz=UTC) - timedelta(days=65)
    results = [make_result(idx, published) for idx in range(60)]
    client = StubExaClient(results)
    output = tmp_path / "exa_seed.json"

    records = exa_discovery.run_pipeline(
        days_min=60,
        days_max=90,
        limit=80,
        output_file=output,
        client=client,
    )

    assert client.calls == 1
    assert len(records) == len(results)
    assert output.exists()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload) == len(results)
    first = payload[0]
    assert first["exa_found"] is True
    assert first["funding_stage"] == "Series A"
    assert first["funding_amount"] == 12_000_000
    assert "discovered_at" in first

    # Running a second time should upsert (no duplicate entries)
    exa_discovery.run_pipeline(
        days_min=60,
        days_max=90,
        limit=80,
        output_file=output,
        client=client,
    )
    payload_after = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload_after) == len(results)


def test_discover_with_retries_handles_rate_limit(monkeypatch):
    captured_sleeps = []

    def fake_sleep(seconds: float):
        captured_sleeps.append(seconds)

    monkeypatch.setattr(exa_discovery.time, "sleep", fake_sleep)

    class FlakyClient:
        def __init__(self):
            self._attempts = 0

        def search_recent_funding(self, **kwargs):
            self._attempts += 1
            if self._attempts == 1:
                raise ExaRateLimitError()
            return [
                {
                    "title": "Acme raises $1M Seed",
                    "summary": "Seed",
                    "text": "Seed",
                    "url": "https://example.com",
                    "publishedDate": datetime.now(tz=UTC).isoformat(),
                }
            ]

    client = FlakyClient()
    results = exa_discovery.discover_with_retries(
        client,
        query="test",
        days_min=60,
        days_max=90,
        limit=10,
        max_attempts=3,
    )

    assert len(results) == 1
    assert len(captured_sleeps) == 1
