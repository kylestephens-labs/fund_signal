"""Fixture bundle resolution and verification."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pipelines.news_client import (
    FIXTURE_DIR_ENV,
    FixtureSource,
    RuntimeConfig,
    RuntimeMode,
    get_runtime_config,
)
from tools import verify_bundle
from tools.verify_bundle import VerificationError

logger = logging.getLogger("pipelines.fixture_reader")

FIXTURE_ROOT_ENV = "FUND_SIGNAL_FIXTURE_ROOT"
LOCAL_SAMPLE_ROOT = Path("fixtures/sample")
SUPABASE_ROOT = Path("fixtures/latest")
LATEST_FILENAME = "latest.json"


class FixtureError(RuntimeError):
    """Raised when fixture bundle resolution fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class BundleInfo:
    """Metadata describing a resolved fixture bundle."""

    bundle_id: str
    path: Path
    captured_at: datetime
    expiry_days: int
    manifest: dict

    @property
    def fixtures_dir(self) -> Path:
        return self.path / "fixtures"

    @property
    def leads_dir(self) -> Path:
        return self.path / "leads"

    @property
    def raw_dir(self) -> Path:
        return self.path / "raw"


BundleDir = Literal["fixtures_dir", "leads_dir", "raw_dir"]


@dataclass(frozen=True)
class FixtureArtifactSpec:
    """Defines how to swap a CLI path with the bundle artifact."""

    default_path: Path
    location: BundleDir
    filename: str | None = None

    def matches(self, path: Path) -> bool:
        return path == self.default_path

    def resolve(self, bundle: BundleInfo) -> Path:
        base_dir = getattr(bundle, self.location)
        target_name = self.filename or self.default_path.name
        return base_dir / target_name


@dataclass(frozen=True)
class BundleContext:
    """Resolved bundle metadata plus any swapped paths."""

    input_path: Path
    output_path: Path
    bundle: BundleInfo | None = None
    scoring_timestamp: datetime | None = None

    @property
    def has_bundle(self) -> bool:
        return self.bundle is not None


def ensure_bundle(runtime_mode: RuntimeMode | None = None) -> BundleInfo:
    """Resolve and verify the current bundle (fixture mode only)."""
    config = get_runtime_config()
    config_mode = runtime_mode or config.mode
    if config_mode is not RuntimeMode.FIXTURE:
        raise FixtureError("E_MODE_UNSUPPORTED", "Bundle resolution requires fixture mode.")

    root = resolve_fixture_root(config)
    latest_path = root / LATEST_FILENAME
    pointer = _load_latest_pointer(latest_path)
    bundle_path = _resolve_bundle_path(pointer, latest_path.parent)
    bundle = _load_bundle(bundle_path)
    os.environ[FIXTURE_DIR_ENV] = str(bundle.fixtures_dir)
    return bundle


def log_bundle(bundle: BundleInfo) -> None:
    age_days = (datetime.now(UTC) - bundle.captured_at).total_seconds() / 86400
    logger.info(
        "Using bundle %s captured %s (age %.2fd, expiry %sd).",
        bundle.bundle_id,
        bundle.captured_at.isoformat(),
        age_days,
        bundle.expiry_days,
    )


@dataclass(frozen=True)
class LatestPointer:
    """Representation of latest.json pointer."""

    bundle_prefix: str

    @classmethod
    def from_path(cls, latest_path: Path) -> LatestPointer:
        if not latest_path.exists():
            raise FixtureError("E_LATEST_MISSING", f"latest.json not found at {latest_path}")
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        bundle_prefix = payload.get("bundle_prefix")
        if not bundle_prefix:
            raise FixtureError("E_LATEST_INVALID", "latest.json missing bundle_prefix.")
        return cls(bundle_prefix=bundle_prefix)


def _load_latest_pointer(latest_path: Path) -> LatestPointer:
    return LatestPointer.from_path(latest_path)


def _resolve_bundle_path(pointer: LatestPointer, base_dir: Path) -> Path:
    bundle_path = Path(pointer.bundle_prefix)
    if not bundle_path.is_absolute():
        bundle_path = (base_dir / bundle_path).resolve()
    return bundle_path


@lru_cache(maxsize=8)
def _load_bundle(bundle_path: Path) -> BundleInfo:
    manifest_path = bundle_path / "manifest.json"
    if not manifest_path.exists():
        raise FixtureError("E_FIXTURE_NOT_FOUND", f"Manifest missing at {manifest_path}")
    try:
        verify_bundle.verify_manifest(manifest_path)
    except VerificationError as exc:
        raise FixtureError(exc.code, str(exc)) from exc
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    captured_at = verify_bundle.parse_timestamp(manifest["captured_at"])
    return BundleInfo(
        bundle_id=manifest["bundle_id"],
        path=bundle_path,
        captured_at=captured_at,
        expiry_days=manifest["expiry_days"],
        manifest=manifest,
    )


def clear_bundle_cache() -> None:
    """Testing helper to clear cached bundle metadata."""
    _load_bundle.cache_clear()


def resolve_bundle_context(
    config: RuntimeConfig,
    *,
    input_path: Path,
    output_path: Path,
    input_spec: FixtureArtifactSpec | None = None,
    output_spec: FixtureArtifactSpec | None = None,
    log: bool = True,
) -> BundleContext:
    """Return resolved I/O paths plus bundle metadata when defaults are used."""
    if config.mode is not RuntimeMode.FIXTURE:
        return BundleContext(input_path=input_path, output_path=output_path, bundle=None, scoring_timestamp=None)

    needs_bundle = any(
        spec and spec.matches(path)
        for spec, path in (
            (input_spec, input_path),
            (output_spec, output_path),
        )
    )
    bundle: BundleInfo | None = None
    if needs_bundle:
        try:
            bundle = ensure_bundle(config.mode)
        except FixtureError:
            if needs_bundle:
                raise
            bundle = None

    if not needs_bundle or not bundle:
        return BundleContext(
            input_path=input_path,
            output_path=output_path,
            bundle=bundle,
            scoring_timestamp=bundle.captured_at if bundle else None,
        )

    if log:
        log_bundle(bundle)

    resolved_input = input_spec.resolve(bundle) if input_spec and input_spec.matches(input_path) else input_path
    resolved_output = output_spec.resolve(bundle) if output_spec and output_spec.matches(output_path) else output_path

    return BundleContext(
        input_path=resolved_input,
        output_path=resolved_output,
        bundle=bundle,
        scoring_timestamp=bundle.captured_at,
    )


def resolve_fixture_root(config) -> Path:
    explicit = os.getenv(FIXTURE_ROOT_ENV)
    if explicit:
        return Path(explicit).expanduser()
    if config.source is FixtureSource.LOCAL:
        return LOCAL_SAMPLE_ROOT
    if config.source is FixtureSource.SUPABASE:
        return SUPABASE_ROOT
    return SUPABASE_ROOT
