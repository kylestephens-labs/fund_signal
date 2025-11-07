import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.verify_bundle import VerificationError, verify_manifest


def build_manifest(tmp_path: Path, *, age_days: float) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    data_path = bundle / "fixtures" / "youcom" / "articles.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "[]"
    data_path.write_text(payload, encoding="utf-8")
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    manifest = {
        "schema_version": 1,
        "bundle_id": "bundle-test",
        "captured_at": (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(),
        "expiry_days": 7,
        "git_commit": None,
        "tool_version": "1.0.0",
        "providers": [],
        "files": [
            {
                "path": data_path.relative_to(bundle).as_posix(),
                "size": len(payload),
                "checksum": checksum,
            }
        ],
        "signature": None,
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_fresh_bundle_passes(tmp_path: Path):
    manifest = build_manifest(tmp_path, age_days=1)
    verify_manifest(manifest)


def test_expired_bundle_fails(tmp_path: Path):
    manifest = build_manifest(tmp_path, age_days=10)
    with pytest.raises(VerificationError) as excinfo:
        verify_manifest(manifest)
    assert excinfo.value.code == "E_BUNDLE_EXPIRED"
