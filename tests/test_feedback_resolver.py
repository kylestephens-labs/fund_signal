import hashlib
import json
from pathlib import Path

from tools import verify_feedback_resolver
from tools.manifest_utils import compute_sha256

FIXTURE_DIR = Path("tests/fixtures/bundles/feedback_case/leads")


def test_feedback_resolver_promotes_entity(tmp_path: Path):
    normalized = tmp_path / "exa_seed.normalized.json"
    normalized.write_text((FIXTURE_DIR / "exa_seed.normalized.json").read_text(), encoding="utf-8")
    youcom = FIXTURE_DIR / "youcom_verified.json"
    tavily = FIXTURE_DIR / "tavily_verified.json"
    output = tmp_path / "exa_seed.feedback_resolved.json"

    summary = verify_feedback_resolver.apply_feedback(normalized, output, youcom, tavily)

    assert summary["feedback_applied"] == 1
    payload = verify_feedback_resolver.load_json(output)
    rows = payload["data"]
    updated = next(row for row in rows if row["id"] == "row_hotglue")
    assert updated["company_name"] == "Hotglue"
    assert updated["feedback_applied"] is True
    assert updated["feedback_domains"] == ["businesswire.com", "techcrunch.com", "venturebeat.com"]
    assert payload["feedback_version"] == "v1"
    assert payload["feedback_sha256"]
    assert updated["feedback_sha256"]


def test_feedback_resolver_is_deterministic(tmp_path: Path):
    normalized = tmp_path / "exa_seed.normalized.json"
    normalized.write_text((FIXTURE_DIR / "exa_seed.normalized.json").read_text(), encoding="utf-8")
    youcom = FIXTURE_DIR / "youcom_verified.json"
    tavily = FIXTURE_DIR / "tavily_verified.json"
    output = tmp_path / "exa_seed.feedback_resolved.json"

    verify_feedback_resolver.apply_feedback(normalized, output, youcom, tavily)
    first_bytes = output.read_bytes()
    verify_feedback_resolver.apply_feedback(normalized, output, youcom, tavily)
    assert first_bytes == output.read_bytes()


def test_feedback_resolver_updates_manifest(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    leads_dir = bundle_dir / "leads"
    leads_dir.mkdir(parents=True, exist_ok=True)

    normalized = leads_dir / "exa_seed.normalized.json"
    normalized.write_text((FIXTURE_DIR / "exa_seed.normalized.json").read_text(), encoding="utf-8")
    youcom = FIXTURE_DIR / "youcom_verified.json"
    tavily = FIXTURE_DIR / "tavily_verified.json"
    output = leads_dir / "exa_seed.feedback_resolved.json"

    manifest_path = bundle_dir / "manifest.json"
    manifest = {
        "bundle_id": "bundle-test",
        "captured_at": "2025-11-10T00:00:00Z",
        "files": [{"path": "leads/exa_seed.feedback_resolved.json", "sha256": "placeholder"}],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    summary = verify_feedback_resolver.apply_feedback(
        normalized,
        output,
        youcom,
        tavily,
        manifest_path=manifest_path,
    )

    assert summary["output_sha256"] == compute_sha256(output)
    updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in updated_manifest["files"]
        if item["path"] == "leads/exa_seed.feedback_resolved.json"
    )
    assert entry["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
