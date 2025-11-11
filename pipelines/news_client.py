"""Runtime client selection for online vs. fixture modes."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.clients.tavily import TavilyClient
from app.clients.youcom import YoucomClient

logger = logging.getLogger("pipelines.news_client")

MODE_ENV = "FUND_SIGNAL_MODE"
SOURCE_ENV = "FUND_SIGNAL_SOURCE"
FIXTURE_DIR_ENV = "FUND_SIGNAL_FIXTURE_DIR"
SUPABASE_BASE_URL_ENV = "FUND_SIGNAL_SUPABASE_BASE_URL"
SUPABASE_TOKEN_ENV = "FUND_SIGNAL_SUPABASE_SERVICE_KEY"  # noqa: S105 - env var name, not a secret value


class RuntimeMode(str, Enum):
    """Available runtime behaviors."""

    ONLINE = "online"
    FIXTURE = "fixture"


class FixtureSource(str, Enum):
    """Where fixtures are loaded from."""

    LOCAL = "local"
    SUPABASE = "supabase"


class ModeError(RuntimeError):
    """Raised when runtime mode/source configuration is invalid."""

    def __init__(self, message: str, code: str = "E_MODE_UNSUPPORTED") -> None:
        super().__init__(message)
        self.code = code


class FixtureNotFoundError(ModeError):
    """Raised when a requested fixture artifact cannot be located."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Fixture not found: {path}", code="E_FIXTURE_NOT_FOUND")
        self.path = path


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved runtime configuration."""

    mode: RuntimeMode
    source: FixtureSource
    fixture_base: Path | None = None


class YoucomClientProtocol(Protocol):
    """Subset of You.com client behavior used by the pipeline."""

    def search_news(self, *, query: str, limit: int, time_filter: str | None = None) -> list[dict[str, Any]]:
        ...


class TavilyClientProtocol(Protocol):
    """Subset of Tavily client behavior used by the pipeline."""

    def search(self, *, query: str, max_results: int, days_limit: int | None = None) -> list[dict[str, Any]]:
        ...


def _parse_mode(value: str | None, *, default: RuntimeMode) -> RuntimeMode:
    return _parse_enum(RuntimeMode, value, default=default, env_var=MODE_ENV)


def _parse_source(value: str | None, *, default: FixtureSource) -> FixtureSource:
    return _parse_enum(FixtureSource, value, default=default, env_var=SOURCE_ENV)


def _parse_enum(enum_cls: type[Enum], value: str | None, *, default: Enum, env_var: str) -> Enum:
    if not value:
        return default
    normalized = value.strip().lower()
    for enum_value in enum_cls:
        if normalized == enum_value.value:
            return enum_value
    raise ModeError(f"Unsupported {env_var} value: {value}")


_LOGGED_CONFIG = False


def get_runtime_config() -> RuntimeConfig:
    """Resolve runtime configuration from environment variables."""
    global _LOGGED_CONFIG  # noqa: PLW0603
    mode = _parse_mode(os.getenv(MODE_ENV), default=RuntimeMode.FIXTURE)
    source = _parse_source(os.getenv(SOURCE_ENV), default=FixtureSource.LOCAL)
    fixture_base: Path | None = None

    if mode is RuntimeMode.FIXTURE and source is FixtureSource.LOCAL:
        fixture_dir = os.getenv(FIXTURE_DIR_ENV, "fixtures/sample")
        fixture_base = Path(fixture_dir).expanduser()

    config = RuntimeConfig(mode=mode, source=source, fixture_base=fixture_base)
    if not _LOGGED_CONFIG:
        logger.info("FundSignal runtime mode=%s source=%s", config.mode.value, config.source.value)
        _LOGGED_CONFIG = True
    return config


def get_youcom_client(config: RuntimeConfig | None = None) -> YoucomClientProtocol:
    """Return an appropriate You.com client implementation."""
    config = config or get_runtime_config()
    if config.mode is RuntimeMode.FIXTURE:
        store = _build_fixture_store(config)
        return FixtureYoucomClient(store)
    return YoucomClient.from_env()


def get_tavily_client(config: RuntimeConfig | None = None) -> TavilyClientProtocol:
    """Return an appropriate Tavily client implementation."""
    config = config or get_runtime_config()
    if config.mode is RuntimeMode.FIXTURE:
        store = _build_fixture_store(config)
        return FixtureTavilyClient(store)
    return TavilyClient.from_env()


class FixtureStore(Protocol):
    """Common interface for fixture stores."""

    def load_json(self, relative_path: str) -> Any:
        ...


def _build_fixture_store(config: RuntimeConfig) -> FixtureStore:
    if config.source is FixtureSource.LOCAL:
        if not config.fixture_base:
            raise ModeError("Fixture base path is required for local source.")
        return LocalFixtureStore(config.fixture_base)
    if config.source is FixtureSource.SUPABASE:
        base_url = os.getenv(SUPABASE_BASE_URL_ENV)
        token = os.getenv(SUPABASE_TOKEN_ENV)
        if not base_url:
            raise ModeError(
                f"{SUPABASE_BASE_URL_ENV} must be set when FUND_SIGNAL_SOURCE=supabase.",
            )
        return SupabaseFixtureStore(base_url=base_url, token=token)
    raise ModeError(f"Unsupported fixture source: {config.source.value}")


class LocalFixtureStore:
    """Loads fixtures from the repository tree."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def load_json(self, relative_path: str) -> Any:
        target = (self._base_dir / relative_path).resolve()
        if not target.exists():
            raise FixtureNotFoundError(str(target))
        with target.open("r", encoding="utf-8") as infile:
            return json.load(infile)


class SupabaseFixtureStore:
    """Loads fixtures from a Supabase storage bucket (HTTP)."""

    def __init__(self, *, base_url: str, token: str | None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    def load_json(self, relative_path: str) -> Any:
        url = f"{self._base_url}/{relative_path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = httpx.get(url, timeout=15, headers=headers)
        if response.status_code == 404:
            raise FixtureNotFoundError(url)
        response.raise_for_status()
        return response.json()


class _FixtureClientBase:
    """Shared helpers for fixture-backed clients."""

    def __init__(self, store: FixtureStore, artifact: str) -> None:
        self._store = store
        self._artifact = artifact

    @cached_property
    def _articles(self) -> Sequence[dict[str, Any]]:
        payload = self._store.load_json(self._artifact)
        if not isinstance(payload, list):
            raise FixtureNotFoundError(self._artifact)
        return payload

    @staticmethod
    def _bounded(items: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return list(items[:limit])


class FixtureYoucomClient(_FixtureClientBase):
    """Fixture-backed You.com client."""

    def __init__(self, store: FixtureStore, artifact: str = "youcom/articles.json") -> None:
        super().__init__(store, artifact)

    def search_news(
        self,
        *,
        query: str,
        limit: int,
        time_filter: str | None = None,  # noqa: ARG002 - signature parity
    ) -> list[dict[str, Any]]:
        _ = query  # Fixtures are static snapshots.
        return self._bounded(self._articles, limit)


class FixtureTavilyClient(_FixtureClientBase):
    """Fixture-backed Tavily client."""

    def __init__(self, store: FixtureStore, artifact: str = "tavily/articles.json") -> None:
        super().__init__(store, artifact)

    def search(
        self,
        *,
        query: str,
        max_results: int,
        days_limit: int | None = None,  # noqa: ARG002 - signature parity
    ) -> list[dict[str, Any]]:
        _ = query
        return self._bounded(self._articles, max_results)
