import json
import os
from contextlib import ExitStack
from pathlib import Path

import pytest

from app.clients.exa import ExaClient
from app.clients.tavily import TavilyClient
from app.clients.youcom import YoucomClient


@pytest.mark.contract
def test_online_contract_smoke():
    required = ["EXA_API_KEY", "YOUCOM_API_KEY", "TAVILY_API_KEY"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        pytest.skip(f"Missing API keys: {', '.join(missing)}")

    data_path = Path(__file__).parent / "data" / "contract_queries.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))

    with ExitStack() as stack:
        exa_client = stack.enter_context(ExaClient.from_env())
        youcom_client = stack.enter_context(YoucomClient.from_env())
        tavily_client = stack.enter_context(TavilyClient.from_env())

        exa_results = exa_client.search_recent_funding(
            query=data["exa"]["query"],
            days_min=data["exa"]["days_min"],
            days_max=data["exa"]["days_max"],
            limit=data["exa"]["limit"],
        )
        assert isinstance(exa_results, list)

        youcom_results = youcom_client.search_news(
            query=data["youcom"]["query"],
            limit=data["youcom"]["limit"],
        )
        assert all("url" in item for item in youcom_results)

        tavily_results = tavily_client.search(
            query=data["tavily"]["query"],
            max_results=data["tavily"]["max_results"],
        )
        assert all("url" in item for item in tavily_results)
