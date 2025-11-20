import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools import capture_pipeline, verify_bundle
from tools.verify_bundle import VerificationError


def write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def create_manifest(bundle: Path, *, age_days: int = 0, signature: bool = False) -> Path:
    fixtures_dir = bundle / "fixtures"
    write_file(fixtures_dir / "youcom/articles.json", "[]")
    write_file(fixtures_dir / "tavily/articles.json", "[]")
    leads_dir = bundle / "leads"
    write_file(leads_dir / "youcom_verified.json", "[]")
    write_file(leads_dir / "tavily_confirmed.json", "[]")

    captured_at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    manifest = {
        "schema_version": 1,
        "bundle_id": bundle.name,
        "captured_at": captured_at,
        "expiry_days": 7,
        "git_commit": None,
        "tool_version": "1.0.0",
        "providers": [],
        "files": capture_pipeline.gather_file_metadata(bundle),
    }
    manifest["signature"] = None
    if signature:
        manifest_no_sig = dict(manifest)
        manifest_no_sig.pop("signature", None)
        manifest["signature"] = capture_pipeline.sign_manifest(manifest_no_sig, key="secret")
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def test_verify_manifest_success(tmp_path: Path):
    bundle = tmp_path / "bundle"
    manifest_path = create_manifest(bundle)
    verify_bundle.verify_manifest(manifest_path)


def test_verify_manifest_expired(tmp_path: Path):
    bundle = tmp_path / "bundle-expired"
    manifest_path = create_manifest(bundle, age_days=9)
    with pytest.raises(VerificationError) as excinfo:
        verify_bundle.verify_manifest(manifest_path)
    assert excinfo.value.code == "E_BUNDLE_EXPIRED"


def test_verify_manifest_checksum_mismatch(tmp_path: Path):
    bundle = tmp_path / "bundle-bad"
    manifest_path = create_manifest(bundle)
    bad_file = bundle / "fixtures/youcom/articles.json"
    bad_file.write_text('[{"tampered": true}]', encoding="utf-8")
    with pytest.raises(VerificationError) as excinfo:
        verify_bundle.verify_manifest(manifest_path)
    assert excinfo.value.code == "E_CHECKSUM_MISMATCH"


def test_verify_manifest_signature(tmp_path: Path, monkeypatch):
    bundle = tmp_path / "bundle-signed"
    manifest_path = create_manifest(bundle, signature=True)
    monkeypatch.setenv("BUNDLE_HMAC_KEY", "secret")
    verify_bundle.verify_manifest(manifest_path)

    monkeypatch.setenv("BUNDLE_HMAC_KEY", "wrong")
    with pytest.raises(VerificationError) as excinfo:
        verify_bundle.verify_manifest(manifest_path)
    assert excinfo.value.code == "E_SIGNATURE_MISMATCH"
