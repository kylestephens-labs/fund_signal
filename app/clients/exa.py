"""Client for interacting with the Exa semantic search API."""

from __future__ import annotations

import os
from typing import Any

import httpx


class ExaError(RuntimeError):
    """Base error for Exa client failures."""

    def __init__(self, message: str, code: str = "EXA_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ExaRateLimitError(ExaError):
    """Raised when Exa responds with HTTP 429."""

    def __init__(self, message: str = "Rate limited by Exa") -> None:
        super().__init__(message, code="EXA_429")


class ExaTimeoutError(ExaError):
    """Raised when Exa request times out."""

    def __init__(self, message: str = "Exa request timed out") -> None:
        super().__init__(message, code="EXA_TIMEOUT")


class ExaSchemaError(ExaError):
    """Raised when Exa response schema is not as expected."""

    def __init__(self, message: str = "Unexpected Exa response schema") -> None:
        super().__init__(message, code="EXA_SCHEMA_ERR")


class ExaClient:
    """Minimal Exa API client."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.exa.ai",
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("EXA_API_KEY is required to create an ExaClient.")
        self._api_key = api_key
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    @classmethod
    def from_env(cls) -> "ExaClient":
        """Instantiate the client using the EXA_API_KEY environment variable."""
        api_key = os.getenv("EXA_API_KEY", "")
        return cls(api_key)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._owns_http_client:
            self._http.close()

    def search_recent_funding(
        self,
        *,
        query: str,
        days_min: int,
        days_max: int,
        limit: int,
        autoprompt: bool = True,
    ) -> list[dict[str, Any]]:
        """Query Exa for recent funding announcements."""
        if limit <= 0:
            raise ValueError("limit must be a positive integer.")

        payload = {
            "query": query,
            "num_results": limit,
            "type": "neural",
            "use_autoprompt": autoprompt,
            "contents": {
                "text": {"max_characters": 2000},
                "summary": True,
            },
        }
        headers = {"X-API-KEY": self._api_key}

        try:
            response = self._http.post("/search", json=payload, headers=headers)
        except httpx.TimeoutException as exc:  # pragma: no cover - network failures
            raise ExaTimeoutError() from exc
        except httpx.HTTPError as exc:  # pragma: no cover - network failures
            raise ExaError(f"HTTP error calling Exa: {exc}") from exc

        if response.status_code == 429:
            raise ExaRateLimitError()

        if response.status_code in (408, 504):
            raise ExaTimeoutError()

        if response.status_code >= 400:
            detail: str | None = None
            try:
                payload = response.json()
                detail = payload.get("message") or payload.get("detail")
            except Exception:  # pragma: no cover - best effort decoding
                detail = response.text[:200]
            message = f"Exa request failed: {response.status_code}"
            if detail:
                message = f"{message} - {detail}"
            raise ExaError(
                message,
                code=response.headers.get("x-exa-error-code", "EXA_ERROR"),
            )

        try:
            data = response.json()
        except ValueError as exc:  # pragma: no cover - invalid JSON response
            raise ExaSchemaError("Failed to decode Exa response JSON.") from exc
        results = data.get("results")

        if not isinstance(results, list):
            raise ExaSchemaError("`results` missing from Exa response.")

        if not all(isinstance(entry, dict) for entry in results):
            raise ExaSchemaError("Entries in `results` must be JSON objects.")

        return results

    def __enter__(self) -> "ExaClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
