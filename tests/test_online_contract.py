import json
import os
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.clients.exa import ExaClient
from app.clients.tavily import TavilyClient, TavilyQuotaExceededError
from app.clients.youcom import YoucomClient

CONTRACT_DATA_PATH = Path(__file__).parent / "data" / "contract_queries.json"
_REQUIRED_KEYS = ("EXA_API_KEY", "YOUCOM_API_KEY", "TAVILY_API_KEY")


@dataclass(frozen=True)
class ContractQueries:
    """Typed contract query definitions loaded from disk."""

    exa: Mapping[str, Any]
    youcom: Mapping[str, Any]
    tavily: Mapping[str, Any]

    @classmethod
    def load(cls, path: Path) -> "ContractQueries":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            exa=_require_fields("exa", payload, {"query", "days_min", "days_max", "limit"}),
            youcom=_require_fields("youcom", payload, {"query", "limit"}),
            tavily=_require_fields("tavily", payload, {"query", "max_results"}),
        )


def _require_fields(name: str, payload: Mapping[str, Any], required: set[str]) -> Mapping[str, Any]:
    section = payload.get(name)
    if not isinstance(section, Mapping):
        raise ValueError(f"Contract query '{name}' missing or invalid.")
    missing = required - section.keys()
    if missing:
        raise ValueError(
            f"Contract query '{name}' missing required fields: {', '.join(sorted(missing))}"
        )
    return section


def _require_api_keys(keys: Sequence[str]) -> None:
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        pytest.skip(f"Missing API keys: {', '.join(missing)}")


@pytest.fixture(scope="module")
def contract_queries() -> ContractQueries:
    return ContractQueries.load(CONTRACT_DATA_PATH)


@pytest.mark.contract
def test_online_contract_smoke(contract_queries: ContractQueries):
    _require_api_keys(_REQUIRED_KEYS)

    with ExitStack() as stack:
        exa_client = stack.enter_context(ExaClient.from_env())
        youcom_client = stack.enter_context(YoucomClient.from_env())
        tavily_client = stack.enter_context(TavilyClient.from_env())

        exa_results = exa_client.search_recent_funding(**contract_queries.exa)
        assert isinstance(exa_results, list)
        assert exa_results, "Exa contract query returned no results."

        youcom_results = youcom_client.search_news(**contract_queries.youcom)
        assert youcom_results and all("url" in item for item in youcom_results)

        try:
            tavily_results = tavily_client.search(**contract_queries.tavily)
        except TavilyQuotaExceededError as exc:
            pytest.skip(f"Tavily quota exhausted in contract test: {exc}")
        assert tavily_results and all("url" in item for item in tavily_results)
