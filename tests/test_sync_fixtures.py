import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools import sync_fixtures


def create_local_source(tmp_path: Path, *, age_days: int = 0, tamper: bool = False) -> Path:
    root = tmp_path / "source"
    bundle = root / "bundle-sample"
    (bundle / "fixtures" / "youcom").mkdir(parents=True, exist_ok=True)
    (bundle / "fixtures" / "tavily").mkdir(parents=True, exist_ok=True)
    (bundle / "leads").mkdir(parents=True, exist_ok=True)
    (bundle / "raw").mkdir(parents=True, exist_ok=True)
    paths = [
        bundle / "fixtures/youcom/articles.json",
        bundle / "fixtures/tavily/articles.json",
        bundle / "leads/youcom_verified.json",
        bundle / "leads/tavily_confirmed.json",
    ]
    for path in paths:
        file_path = bundle / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("[]", encoding="utf-8")

    captured_at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    files = []
    for path in paths:
        file_path = bundle / path
        data = file_path.read_text(encoding="utf-8").encode("utf-8")
        files.append(
            {
                "path": file_path.relative_to(bundle).as_posix(),
                "size": len(data),
                "checksum": hashlib.sha256(data).hexdigest(),
            }
        )

    manifest = {
        "schema_version": 1,
        "bundle_id": "bundle-sample",
        "captured_at": captured_at,
        "expiry_days": 7,
        "git_commit": None,
        "tool_version": "1.0.0",
        "providers": [],
        "files": files,
        "signature": None,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "latest.json").write_text(
        json.dumps({"bundle_id": "bundle-sample", "bundle_prefix": "bundle-sample"}),
        encoding="utf-8",
    )
    if tamper:
        (bundle / "leads/youcom_verified.json").write_text("[42]", encoding="utf-8")
    return root


def make_args(**overrides):
    defaults = dict(
        source="local",
        dest=Path("fixtures/latest"),
        local_root=Path("fixtures/sample"),
        supabase_prefix="artifacts",
        supabase_bucket="fundsignal-artifacts",
        pointer="latest.json",
    )
    defaults.update(overrides)

    class Args:
        def __init__(self, **entries):
            self.__dict__.update(entries)

    return Args(**defaults)


def test_sync_local_success(tmp_path: Path):
    source_root = create_local_source(tmp_path)
    dest = tmp_path / "fixtures" / "latest"
    args = make_args(source="local", dest=dest, local_root=source_root)
    bundle = sync_fixtures.sync(args)
    assert bundle.bundle_dir.exists()
    assert (dest / "latest.json").exists()


def test_sync_local_expired(tmp_path: Path):
    source_root = create_local_source(tmp_path, age_days=10)
    dest = tmp_path / "fixtures" / "latest"
    args = make_args(source="local", dest=dest, local_root=source_root)
    with pytest.raises(sync_fixtures.SyncError) as excinfo:
        sync_fixtures.sync(args)
    assert excinfo.value.code == "E_BUNDLE_EXPIRED"
