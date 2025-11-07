import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from pipelines.day1 import confidence_scoring, tavily_confirm, youcom_verify


@dataclass(frozen=True)
class PipelineArtifacts:
    youcom: Path
    tavily: Path
    confidence: Path

    def read(self) -> tuple[list[dict], list[dict], list[dict]]:
        youcom_records = _load_json_array(self.youcom)
        tavily_records = _load_json_array(self.tavily)
        confidence_records = _load_json_array(self.confidence)
        return youcom_records, tavily_records, confidence_records


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


def _load_json_array(path: Path) -> list[dict]:
    assert path.exists(), f"expected artifact {path} to exist"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, f"artifact {path} is empty or not a list"
    return data


def _load_manifest(bundle_root: Path) -> dict:
    manifest_path = bundle_root / "bundle-sample" / "manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _assert_confidence_fields(confidence_records: list[dict], manifest: dict) -> None:
    for record in confidence_records:
        assert record["freshness_watermark"]
        last_checked = record["last_checked_at"]
        assert isinstance(last_checked, str) and last_checked, "last_checked_at missing"
        assert record["verified_by"]
        assert record["confidence"] in {"VERIFIED", "LIKELY"}


def test_pipelines_run_offline_with_fixtures(fixture_env, tmp_path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    artifacts = PipelineArtifacts(
        youcom=outputs_dir / "youcom.json",
        tavily=outputs_dir / "tavily.json",
        confidence=outputs_dir / "confidence.json",
    )

    youcom_verify.run_pipeline(
        input_path=youcom_verify.DEFAULT_INPUT,
        output_path=artifacts.youcom,
        min_articles=2,
        max_results=4,
    )
    tavily_confirm.run_pipeline(
        input_path=tavily_confirm.DEFAULT_INPUT,
        output_path=artifacts.tavily,
        min_confirmations=2,
        max_results=4,
    )
    confidence_scoring.run_pipeline(input_path=artifacts.tavily, output_path=artifacts.confidence)

    youcom_records, tavily_records, confidence_records = artifacts.read()
    manifest = _load_manifest(fixture_env)

    assert all(record["youcom_verified"] for record in youcom_records)
    assert all(record["tavily_verified"] or record["tavily_reason"] for record in tavily_records)
    _assert_confidence_fields(confidence_records, manifest)
