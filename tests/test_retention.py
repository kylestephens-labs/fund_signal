from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import enforce_retention


def _write_manifest(bundle, captured_at: datetime):
    manifest = {
        "bundle_id": "bundle-test",
        "captured_at": captured_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "expiry_days": 7,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _create_bundle(tmp_path: Path, *, age_days: int) -> tuple[Path, Path, Path]:
    root = tmp_path / "artifacts"
    bundle = root / f"bundle-{age_days}"
    raw_dir = bundle / "raw"
    leads_dir = bundle / "leads"
    raw_dir.mkdir(parents=True)
    leads_dir.mkdir(parents=True)
    raw_file = raw_dir / "payload.jsonl.gz"
    canonical_file = leads_dir / "lead.json"
    raw_file.write_bytes(b"data")
    canonical_file.write_text("{}", encoding="utf-8")
    captured_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    _write_manifest(bundle, captured_at)
    return root, raw_file, canonical_file


def test_enforce_retention_deletes_raw_only_when_within_window(tmp_path):
    root, raw_file, canonical_file = _create_bundle(tmp_path, age_days=40)

    result = enforce_retention.enforce_retention(
        root,
        delete=True,
        raw_days=30,
        canonical_days=90,
        now=datetime.now(timezone.utc),
    )

    assert raw_file.exists() is False
    assert canonical_file.exists() is True
    assert result.raw_deleted
    assert not result.canonical_deleted


def test_enforce_retention_deletes_canonical_after_threshold(tmp_path):
    root, raw_file, canonical_file = _create_bundle(tmp_path, age_days=120)

    result = enforce_retention.enforce_retention(
        root,
        delete=True,
        raw_days=30,
        canonical_days=90,
        now=datetime.now(timezone.utc),
    )

    assert not raw_file.exists()
    assert not canonical_file.exists()
    assert result.raw_deleted
    assert result.canonical_deleted


def test_enforce_retention_missing_root_raises(tmp_path):
    with pytest.raises(enforce_retention.RetentionError):
        enforce_retention.enforce_retention(
            tmp_path / "missing",
            delete=False,
            raw_days=30,
            canonical_days=90,
        )
