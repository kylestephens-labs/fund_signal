import hashlib
import json
from pathlib import Path

from tools import verify_feedback_resolver

FIXTURE_DIR = Path("tests/fixtures/bundles/feedback_case/leads")


def test_feedback_cli_is_deterministic(tmp_path: Path, monkeypatch):
    normalized = tmp_path / "exa_seed.normalized.json"
    normalized.write_text((FIXTURE_DIR / "exa_seed.normalized.json").read_text(), encoding="utf-8")
    youcom = FIXTURE_DIR / "youcom_verified.json"
    tavily = FIXTURE_DIR / "tavily_verified.json"
    output = tmp_path / "exa_seed.feedback_resolved.json"

    argv = [
        "--input",
        str(normalized),
        "--youcom",
        str(youcom),
        "--tavily",
        str(tavily),
        "--out",
        str(output),
    ]
    assert verify_feedback_resolver.main(argv) == 0
    first = output.read_bytes()
    assert verify_feedback_resolver.main(argv) == 0
    assert first == output.read_bytes()


def test_feedback_cli_updates_manifest(tmp_path: Path):
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
        "files": [{"path": "leads/exa_seed.feedback_resolved.json", "sha256": "placeholder"}],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    argv = [
        "--input",
        str(normalized),
        "--youcom",
        str(youcom),
        "--tavily",
        str(tavily),
        "--out",
        str(output),
        "--update-manifest",
        str(manifest_path),
    ]
    assert verify_feedback_resolver.main(argv) == 0

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest_data["files"] if item["path"] == "leads/exa_seed.feedback_resolved.json")
    assert entry["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
