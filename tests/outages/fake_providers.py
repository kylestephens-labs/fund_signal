from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from app.clients.exa import ExaError, ExaTimeoutError
from app.clients.tavily import TavilyError, TavilyTimeoutError
from app.clients.youcom import YoucomError, YoucomTimeoutError

DEFAULT_YOUCOM_RESULTS = [
    {
        "url": "https://news.dev/acme",
        "title": "Acme raises Series A",
        "publisher": "News.dev",
        "snippet": "Funding wins.",
    }
]
DEFAULT_TAVILY_RESULTS = [
    {
        "url": "https://press.dev/acme",
        "title": "Acme secures Series A",
        "snippet": "Press coverage.",
    }
]
DEFAULT_EXA_RESULTS = [
    {
        "title": "Acme raises Series A",
        "summary": "Acme raises funding",
        "funding_amount": "$12M",
        "funding_stage": "Series A",
        "funding_date": "2024-11-01",
    }
]


logger = logging.getLogger(__name__)


@dataclass
class ProviderOutageScenario:
    provider: str
    mode: str = "timeout"
    attempts_before_success: int = 0
    delay_ms: int = 0
    status_code: int = 503
    results: list[dict[str, Any]] | None = None

    @classmethod
    def from_env(cls, provider: str) -> ProviderOutageScenario:
        """Load outage defaults from environment variables."""
        mode = os.getenv("PROOF_OUTAGE_MODE", "timeout")
        delay = _read_int_env("PROOF_OUTAGE_DELAY_MS", 0)
        status = _read_int_env("PROOF_OUTAGE_STATUS_CODE", 503)
        attempts = _read_int_env("PROOF_OUTAGE_ATTEMPTS", 2)
        return cls(
            provider=provider,
            mode=mode,
            attempts_before_success=attempts,
            delay_ms=delay,
            status_code=status,
        )


class FakeYoucomClient:
    """Deterministic You.com client fake with outage simulation."""

    def __init__(self, scenario: ProviderOutageScenario) -> None:
        self._scenario = scenario
        self.calls = 0

    def search_news(
        self, *, query: str, limit: int, time_filter: str | None = None
    ) -> list[dict[str, Any]]:
        del query, limit, time_filter
        self.calls += 1
        if (
            self._scenario.mode == "timeout"
            and self.calls <= self._scenario.attempts_before_success
        ):
            raise YoucomTimeoutError()
        if self._scenario.mode == "server_error":
            raise YoucomError(
                f"You.com simulated 5xx (status={self._scenario.status_code})",
                code="YOUCOM_5XX",
            )
        if self._scenario.delay_ms:
            time.sleep(self._scenario.delay_ms / 1000)
        return list(self._scenario.results or DEFAULT_YOUCOM_RESULTS)


class FakeTavilyClient:
    """Deterministic Tavily client fake with delay + outage modes."""

    def __init__(self, scenario: ProviderOutageScenario) -> None:
        self._scenario = scenario
        self.calls = 0
        self.observed_latencies: list[float] = []

    def search(
        self, *, query: str, max_results: int, days_limit: int | None = None
    ) -> list[dict[str, Any]]:
        del query, max_results, days_limit
        self.calls += 1
        if (
            self._scenario.mode == "timeout"
            and self.calls <= self._scenario.attempts_before_success
        ):
            raise TavilyTimeoutError()
        if self._scenario.mode == "server_error":
            raise TavilyError(
                f"Tavily simulated 5xx (status={self._scenario.status_code})",
                code="TAVILY_5XX",
            )
        if self._scenario.mode == "slow" and self._scenario.delay_ms:
            start = time.perf_counter()
            time.sleep(self._scenario.delay_ms / 1000)
            self.observed_latencies.append(time.perf_counter() - start)
        return list(self._scenario.results or DEFAULT_TAVILY_RESULTS)


class FakeExaClient:
    """Deterministic Exa client fake emitting outages."""

    def __init__(self, scenario: ProviderOutageScenario) -> None:
        self._scenario = scenario
        self.calls = 0

    def search_recent_funding(
        self,
        *,
        query: str,
        days_min: int,
        days_max: int,
        limit: int,
        autoprompt: bool = True,
    ) -> list[dict[str, Any]]:
        del query, days_min, days_max, limit, autoprompt
        self.calls += 1
        if (
            self._scenario.mode == "timeout"
            and self.calls <= self._scenario.attempts_before_success
        ):
            raise ExaTimeoutError()
        if self._scenario.mode == "server_error":
            raise ExaError(
                f"Exa simulated 5xx (status={self._scenario.status_code})",
                code="EXA_5XX",
            )
        if self._scenario.delay_ms:
            time.sleep(self._scenario.delay_ms / 1000)
        return list(self._scenario.results or DEFAULT_EXA_RESULTS)


def _read_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %s.", name, value, default)
        return default
