"""Client for interacting with the Tavily search API."""

from __future__ import annotations

import os
from typing import Any

import httpx


class TavilyError(RuntimeError):
    """Base error for Tavily client failures."""

    def __init__(self, message: str, code: str = "TAVILY_ERROR") -> None:
        super().__init__(message)
        self.code = code


class TavilyRateLimitError(TavilyError):
    """Raised when Tavily responds with HTTP 429."""

    def __init__(self, message: str = "Rate limited by Tavily") -> None:
        super().__init__(message, code="TAVILY_429")


class TavilyTimeoutError(TavilyError):
    """Raised when Tavily request times out."""

    def __init__(self, message: str = "Tavily request timed out") -> None:
        super().__init__(message, code="TAVILY_TIMEOUT")


class TavilyNotFoundError(TavilyError):
    """Raised when Tavily returns no results."""

    def __init__(self, message: str = "No Tavily results found") -> None:
        super().__init__(message, code="TAVILY_NOT_FOUND")


class TavilySchemaError(TavilyError):
    """Raised when Tavily response schema does not match expectations."""

    def __init__(self, message: str = "Unexpected Tavily response schema") -> None:
        super().__init__(message, code="TAVILY_SCHEMA_ERR")


class TavilyClient:
    """Minimal Tavily API client wrapper."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.tavily.com",
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("TAVILY_API_KEY is required to create a TavilyClient.")
        self._api_key = api_key
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    @classmethod
    def from_env(cls) -> "TavilyClient":
        """Instantiate the client using the TAVILY_API_KEY environment variable."""
        api_key = os.getenv("TAVILY_API_KEY", "")
        return cls(api_key=api_key)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._owns_http_client:
            self._http.close()

    def search(
        self,
        *,
        query: str,
        max_results: int = 6,
        days_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a Tavily web search request."""
        if max_results <= 0:
            raise ValueError("max_results must be a positive integer.")

        payload: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",
            "include_domains": None,
            "max_results": max_results,
        }
        if days_limit:
            payload["days"] = days_limit

        headers = {"X-Tavily-API-Key": self._api_key}

        try:
            response = self._http.post("/search", json=payload, headers=headers)
        except httpx.TimeoutException as exc:  # pragma: no cover - network failures
            raise TavilyTimeoutError() from exc
        except httpx.HTTPError as exc:  # pragma: no cover - network failures
            raise TavilyError(f"HTTP error calling Tavily: {exc}") from exc

        if response.status_code == 429:
            raise TavilyRateLimitError()
        if response.status_code in (408, 504):
            raise TavilyTimeoutError()
        if response.status_code == 404:
            raise TavilyNotFoundError()
        if response.status_code >= 400:
            detail = response.text[:200]
            try:
                detail_json = response.json()
                detail = detail_json.get("message") or detail_json.get("detail") or detail
            except Exception:  # pragma: no cover - best effort
                pass
            raise TavilyError(f"Tavily request failed: {response.status_code} - {detail}")

        try:
            data = response.json()
        except ValueError as exc:  # pragma: no cover - invalid JSON response
            raise TavilySchemaError("Failed to decode Tavily response JSON.") from exc

        results = data.get("results")
        if not isinstance(results, list):
            raise TavilySchemaError("`results` missing from Tavily response.")
        if not all(isinstance(item, dict) for item in results):
            raise TavilySchemaError("Entries in `results` must be JSON objects.")
        return results

    def __enter__(self) -> "TavilyClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
