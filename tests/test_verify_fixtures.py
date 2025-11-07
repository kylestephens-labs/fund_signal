import json
import os
import shutil
from pathlib import Path

import pytest

from pipelines.day1 import confidence_scoring, tavily_confirm, youcom_verify


def _prepare_bundle_fixture(tmp_path: Path) -> Path:
    sample_root = Path("fixtures/sample")
    dest_root = tmp_path / "fixtures" / "latest"
    bundle_src = sample_root / "bundle-sample"
    bundle_dest = dest_root / bundle_src.name
    shutil.copytree(bundle_src, bundle_dest)
    latest_payload = json.loads((sample_root / "latest.json").read_text(encoding="utf-8"))
    (dest_root / "latest.json").write_text(json.dumps(latest_payload), encoding="utf-8")
    return dest_root


@pytest.fixture()
def fixture_env(tmp_path, monkeypatch):
    root = _prepare_bundle_fixture(tmp_path)
    monkeypatch.setenv("FUND_SIGNAL_MODE", "fixture")
    monkeypatch.setenv("FUND_SIGNAL_SOURCE", "local")
    monkeypatch.setenv("FUND_SIGNAL_FIXTURE_ROOT", str(root))
    monkeypatch.setenv("FUND_SIGNAL_FIXTURE_DIR", str(root / "bundle-sample" / "fixtures"))
    return root


def _assert_non_empty_json(path: Path) -> None:
    assert path.exists(), f"expected artifact {path} to exist"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data, f"artifact {path} is empty"


def test_pipelines_run_offline_with_fixtures(fixture_env, tmp_path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    youcom_out = outputs_dir / "youcom.json"
    tavily_out = outputs_dir / "tavily.json"
    confidence_out = outputs_dir / "confidence.json"

    youcom_verify.run_pipeline(
        input_path=youcom_verify.DEFAULT_INPUT,
        output_path=youcom_out,
        min_articles=2,
        max_results=4,
    )
    tavily_confirm.run_pipeline(
        input_path=tavily_confirm.DEFAULT_INPUT,
        output_path=tavily_out,
        min_confirmations=2,
        max_results=4,
    )
    confidence_scoring.run_pipeline(input_path=tavily_out, output_path=confidence_out)

    for path in (youcom_out, tavily_out, confidence_out):
        _assert_non_empty_json(path)
    confidence_payload = json.loads(confidence_out.read_text(encoding="utf-8"))
    manifest = json.loads((fixture_env / "bundle-sample" / "manifest.json").read_text(encoding="utf-8"))
    assert confidence_payload[0]["freshness_watermark"]
    assert confidence_payload[0]["last_checked_at"] == manifest["captured_at"]
