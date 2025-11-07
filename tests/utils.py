"""Test helpers for canonical bundle fixtures."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


def create_canonical_bundle(
    tmp_path: Path,
    *,
    bundle_id: str = "bundle-test",
    captured_at: str = "2025-11-07T03:00:00Z",
    expiry_days: int = 7,
    youcom: Sequence[dict] | None = None,
    tavily: Sequence[dict] | None = None,
    exa: Sequence[dict] | None = None,
    exa_in_raw: bool = False,
) -> Path:
    """Materialize a minimal canonical bundle on disk for testing."""
    root = tmp_path / bundle_id
    leads_dir = root / "leads"
    raw_dir = root / "raw"
    leads_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "bundle_id": bundle_id,
        "schema_version": "1.0",
        "captured_at": captured_at,
        "expiry_days": expiry_days,
        "generated_at": captured_at,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    (leads_dir / "youcom_verified.json").write_text(
        json.dumps(list(youcom or []), indent=2),
        encoding="utf-8",
    )
    (leads_dir / "tavily_confirmed.json").write_text(
        json.dumps(list(tavily or []), indent=2),
        encoding="utf-8",
    )

    if exa:
        target_dir = raw_dir if exa_in_raw else leads_dir
        (target_dir / "exa_seed.json").write_text(json.dumps(list(exa), indent=2), encoding="utf-8")

    return root


def iso_now() -> str:
    """Return a UTC ISO-8601 timestamp with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
