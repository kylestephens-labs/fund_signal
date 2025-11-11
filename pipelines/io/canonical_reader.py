"""Utilities for loading canonical bundle artifacts deterministically."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipelines.io.fixture_loader import BundleInfo


class CanonicalReaderError(RuntimeError):
    """Raised when canonical bundle artifacts cannot be loaded."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CanonicalBundle:
    """Normalized metadata for a canonical artifact bundle."""

    root: Path
    manifest: dict[str, Any]
    captured_at: datetime
    expiry_days: int
    bundle_id: str

    @property
    def leads_dir(self) -> Path:
        return self.root / "leads"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    def leads_path(self, filename: str) -> Path:
        return self.leads_dir / filename

    def raw_path(self, filename: str) -> Path:
        return self.raw_dir / filename


def from_bundle_info(bundle: BundleInfo) -> CanonicalBundle:
    """Build CanonicalBundle metadata from a resolved BundleInfo."""
    return CanonicalBundle(
        root=bundle.path,
        manifest=bundle.manifest,
        captured_at=bundle.captured_at,
        expiry_days=bundle.expiry_days,
        bundle_id=bundle.bundle_id,
    )


def from_path(path: Path) -> CanonicalBundle:
    """Load canonical metadata directly from a filesystem path."""
    root = _resolve_bundle_root(path)
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path, code="E_SCHEMA_INVALID")

    bundle_id = manifest.get("bundle_id")
    captured_at_raw = manifest.get("captured_at")
    expiry_days_raw = manifest.get("expiry_days", 0)
    if not bundle_id:
        raise CanonicalReaderError("E_SCHEMA_INVALID", "manifest.json missing bundle_id.")
    if not captured_at_raw:
        raise CanonicalReaderError("E_TIMESTAMP_INVALID", "manifest.json missing captured_at.")

    try:
        captured_at = _parse_timestamp(captured_at_raw)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise CanonicalReaderError("E_TIMESTAMP_INVALID", str(exc)) from exc

    try:
        expiry_days = int(expiry_days_raw)
    except (TypeError, ValueError) as exc:
        raise CanonicalReaderError("E_SCHEMA_INVALID", "expiry_days must be an integer.") from exc

    return CanonicalBundle(
        root=root,
        manifest=manifest,
        captured_at=captured_at,
        expiry_days=expiry_days,
        bundle_id=bundle_id,
    )


def load_json_array(path: Path, *, required: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Load a canonical JSON array and enforce a minimal schema."""
    if not path.exists():
        raise CanonicalReaderError("E_CANONICAL_INPUT_MISSING", f"Missing canonical artifact: {path}")
    payload = _read_json(path, code="E_CANONICAL_INPUT_MISSING")
    if not isinstance(payload, list):
        raise CanonicalReaderError("E_SCHEMA_INVALID", f"{path} must contain a JSON array.")

    requirements = tuple(required or ())
    for idx, record in enumerate(payload):
        if not isinstance(record, dict):
            raise CanonicalReaderError("E_SCHEMA_INVALID", f"{path} entry {idx} is not an object.")
        for field in requirements:
            if field not in record:
                raise CanonicalReaderError(
                    "E_SCHEMA_INVALID",
                    f"{path} entry {idx} missing required field '{field}'.",
                )
    return payload


def load_sources(bundle: CanonicalBundle) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Load required canonical sources (You.com, Tavily, optional Exa)."""
    youcom = load_json_array(
        bundle.leads_path("youcom_verified.json"),
        required=("company", "press_articles", "youcom_verified"),
    )
    tavily = load_json_array(
        bundle.leads_path("tavily_confirmed.json"),
        required=("company", "proof_links", "tavily_verified"),
    )

    exa_candidates = (
        bundle.leads_path("exa_seed.json"),
        bundle.raw_path("exa_seed.json"),
    )
    for candidate in exa_candidates:
        if candidate.exists():
            exa = load_json_array(candidate, required=("company", "source_url"))
            break
    else:
        exa = []

    return youcom, tavily, exa


def _resolve_bundle_root(path: Path) -> Path:
    path = path.expanduser()
    if not path.exists():
        raise CanonicalReaderError("E_BUNDLE_NOT_FOUND", f"Canonical bundle path not found: {path}")
    if path.is_file():
        if path.name == "manifest.json":
            return path.parent
        if path.name == "latest.json":
            return _resolve_pointer_bundle(path.parent, path)
        maybe_manifest = path.parent / "manifest.json"
        if maybe_manifest.exists():
            return maybe_manifest.parent
        raise CanonicalReaderError("E_BUNDLE_NOT_FOUND", f"Cannot infer bundle root from {path}")

    manifest_path = path / "manifest.json"
    if manifest_path.exists():
        return path

    pointer_path = path / "latest.json"
    if pointer_path.exists():
        return _resolve_pointer_bundle(path, pointer_path)

    raise CanonicalReaderError("E_BUNDLE_NOT_FOUND", f"No manifest.json under {path}")


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _read_json(path: Path, *, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CanonicalReaderError(code, f"{path} contains invalid JSON: {exc}") from exc


def _resolve_pointer_bundle(base_dir: Path, pointer_path: Path) -> Path:
    pointer = _read_json(pointer_path, code="E_BUNDLE_NOT_FOUND")
    prefix = pointer.get("bundle_prefix")
    if not prefix:
        raise CanonicalReaderError("E_BUNDLE_NOT_FOUND", "latest.json missing bundle_prefix.")
    bundle_path = (base_dir / prefix).resolve()
    if (bundle_path / "manifest.json").exists():
        return bundle_path
    raise CanonicalReaderError("E_BUNDLE_NOT_FOUND", f"Bundle {bundle_path} missing manifest.json.")
