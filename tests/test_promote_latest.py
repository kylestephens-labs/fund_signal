import json
from pathlib import Path

from tools import promote_latest


def create_complete_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "artifacts" / "2025" / "11" / "06" / "bundle-123"
    (bundle / "fixtures" / "youcom").mkdir(parents=True, exist_ok=True)
    (bundle / "fixtures" / "tavily").mkdir(parents=True, exist_ok=True)
    (bundle / "leads").mkdir(parents=True, exist_ok=True)
    (bundle / "fixtures" / "youcom" / "articles.json").write_text("[]", encoding="utf-8")
    (bundle / "fixtures" / "tavily" / "articles.json").write_text("[]", encoding="utf-8")
    (bundle / "leads" / "youcom_verified.json").write_text("[]", encoding="utf-8")
    (bundle / "leads" / "tavily_confirmed.json").write_text("[]", encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps({"bundle": "123"}), encoding="utf-8")
    return bundle


def test_promote_writes_latest(tmp_path: Path):
    bundle = create_complete_bundle(tmp_path)
    latest_path = tmp_path / "latest.json"

    promote_latest.promote(bundle, latest_path)

    payload = json.loads(latest_path.read_text(encoding="utf-8"))
    assert payload["bundle_id"] == bundle.name
    assert payload["manifest"]["bundle"] == "123"
    assert any(file["path"] == "manifest.json" for file in payload["files"])


def test_validate_bundle_missing_files(tmp_path: Path):
    bundle = tmp_path / "bundle-incomplete"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")

    try:
        promote_latest.promote(bundle, bundle.parent / "latest.json")
    except FileNotFoundError as exc:
        assert "missing critical files" in str(exc)
    else:  # pragma: no cover - ensure failure occurs
        raise AssertionError("Expected FileNotFoundError for incomplete bundle")
