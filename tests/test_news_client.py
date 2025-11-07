from pathlib import Path

import pytest

from pipelines import news_client
from pipelines.news_client import (
    FixtureNotFoundError,
    ModeError,
    get_runtime_config,
    get_tavily_client,
    get_youcom_client,
)


def test_fixture_mode_reads_local_files(monkeypatch, tmp_path: Path):
    samples_dir = tmp_path / "fixtures" / "sample"
    samples_dir.mkdir(parents=True)
    youcom_path = samples_dir / "youcom" / "articles.json"
    youcom_path.parent.mkdir(parents=True)
    youcom_path.write_text(
        '[{"url":"https://example.com","title":"Sample","snippet":"Snippet","publisher":"Example"}]',
        encoding="utf-8",
    )

    monkeypatch.setenv(news_client.MODE_ENV, "fixture")
    monkeypatch.setenv(news_client.SOURCE_ENV, "local")
    monkeypatch.setenv(news_client.FIXTURE_DIR_ENV, str(samples_dir))

    client = get_youcom_client()
    results = client.search_news(query="anything", limit=1)

    assert len(results) == 1
    assert results[0]["publisher"] == "Example"


def test_online_mode_defers_to_real_clients(monkeypatch):
    class DummyYoucom:
        def search_news(self, *, query: str, limit: int, time_filter=None):
            return [{"url": "https://example.com"}]

    dummy_youcom = DummyYoucom()

    monkeypatch.setenv(news_client.MODE_ENV, "online")
    monkeypatch.setenv(news_client.SOURCE_ENV, "local")

    monkeypatch.setattr(
        news_client.YoucomClient,
        "from_env",
        classmethod(lambda cls: dummy_youcom),
    )

    client = get_youcom_client(get_runtime_config())
    assert client is dummy_youcom


def test_supabase_source_without_base_url_errors(monkeypatch):
    monkeypatch.setenv(news_client.MODE_ENV, "fixture")
    monkeypatch.setenv(news_client.SOURCE_ENV, "supabase")
    monkeypatch.delenv(news_client.SUPABASE_BASE_URL_ENV, raising=False)

    with pytest.raises(ModeError):
        get_youcom_client()


def test_missing_fixture_path_raises(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(news_client.MODE_ENV, "fixture")
    monkeypatch.setenv(news_client.SOURCE_ENV, "local")
    monkeypatch.setenv(news_client.FIXTURE_DIR_ENV, str(tmp_path / "fixtures"))

    with pytest.raises(FixtureNotFoundError):
        get_tavily_client().search(query="acme", max_results=1)
