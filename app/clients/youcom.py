"""Client for interacting with the You.com News API."""

from __future__ import annotations

import os
from typing import Any

import httpx


class YoucomError(RuntimeError):
    """Base error for You.com client failures."""

    def __init__(self, message: str, code: str = "YOUCOM_ERROR") -> None:
        super().__init__(message)
        self.code = code


class YoucomRateLimitError(YoucomError):
    """Raised when You.com responds with HTTP 429."""

    def __init__(self, message: str = "Rate limited by You.com") -> None:
        super().__init__(message, code="YOUCOM_429")


class YoucomTimeoutError(YoucomError):
    """Raised when You.com requests time out."""

    def __init__(self, message: str = "You.com request timed out") -> None:
        super().__init__(message, code="YOUCOM_TIMEOUT")


class YoucomNotFoundError(YoucomError):
    """Raised when You.com cannot find matching articles."""

    def __init__(self, message: str = "No news results found on You.com") -> None:
        super().__init__(message, code="YOUCOM_NOT_FOUND")


class YoucomSchemaError(YoucomError):
    """Raised when You.com response schema is not as expected."""

    def __init__(self, message: str = "Unexpected You.com response schema") -> None:
        super().__init__(message, code="YOUCOM_SCHEMA_ERR")


class YoucomClient:
    """Lightweight You.com API client."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.you.com/v1",
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("YOUCOM_API_KEY is required to create a YoucomClient.")
        self._api_key = api_key
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    @classmethod
    def from_env(cls) -> "YoucomClient":
        """Instantiate the client using the YOUCOM_API_KEY environment variable."""
        api_key = os.getenv("YOUCOM_API_KEY", "")
        return cls(api_key=api_key)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._owns_http_client:
            self._http.close()

    def search_news(
        self,
        *,
        query: str,
        limit: int = 6,
        time_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search You.com News results for the supplied query."""
        if limit <= 0:
            raise ValueError("limit must be a positive integer.")

        payload = {
            "query": query,
            "num_results": limit,
            "type": "news",
        }
        if time_filter:
            payload["time_filter"] = time_filter

        headers = {
            "X-API-Key": self._api_key,
        }

        try:
            response = self._http.post("/search", json=payload, headers=headers)
        except httpx.TimeoutException as exc:  # pragma: no cover - network failure
            raise YoucomTimeoutError() from exc
        except httpx.HTTPError as exc:  # pragma: no cover - network failure
            raise YoucomError(f"HTTP error calling You.com: {exc}") from exc

        if response.status_code == 429:
            raise YoucomRateLimitError()
        if response.status_code in (408, 504):
            raise YoucomTimeoutError()
        if response.status_code == 404:
            raise YoucomNotFoundError()
        if response.status_code >= 400:
            detail = response.text[:200]
            try:
                detail_json = response.json()
                detail = detail_json.get("message") or detail_json.get("detail") or detail
            except Exception:  # pragma: no cover - best effort decoding
                pass
            raise YoucomError(f"You.com request failed: {response.status_code} - {detail}")

        try:
            data = response.json()
        except ValueError as exc:  # pragma: no cover - invalid JSON response
            raise YoucomSchemaError("Failed to decode You.com response JSON.") from exc

        results = data.get("results")
        if not isinstance(results, list):
            raise YoucomSchemaError("`results` missing from You.com response.")
        if not all(isinstance(entry, dict) for entry in results):
            raise YoucomSchemaError("Entries in `results` must be JSON objects.")
        return results

    def __enter__(self) -> "YoucomClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
