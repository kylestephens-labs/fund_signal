import hashlib
import json
from datetime import UTC, datetime, timedelta
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
        "captured_at": (datetime.now(UTC) - timedelta(days=age_days)).isoformat(),
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


@pytest.mark.parametrize(
    ("age_days", "expected_error"),
    (
        (1, None),
        (10, "E_BUNDLE_EXPIRED"),
    ),
)
def test_manifest_freshness_gate(tmp_path: Path, age_days: float, expected_error: str | None):
    manifest = build_manifest(tmp_path, age_days=age_days)
    if expected_error:
        with pytest.raises(VerificationError) as excinfo:
            verify_manifest(manifest)
        assert excinfo.value.code == expected_error
    else:
        verify_manifest(manifest)
