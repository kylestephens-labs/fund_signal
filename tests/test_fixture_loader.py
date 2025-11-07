import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipelines.io import fixture_loader
from pipelines.io.manifest_loader import build_freshness_metadata, WARNING_THRESHOLD
from pipelines.news_client import FixtureSource, RuntimeConfig, RuntimeMode, get_runtime_config
from tools.verify_bundle import VerificationError


def create_bundle(tmp_path: Path, age_days: int = 0) -> Path:
    bundle = tmp_path / "fixtures" / "bundle-sample"
    (bundle / "fixtures" / "youcom").mkdir(parents=True, exist_ok=True)
    (bundle / "fixtures" / "tavily").mkdir(parents=True, exist_ok=True)
    (bundle / "leads").mkdir(parents=True, exist_ok=True)
    (bundle / "raw").mkdir(parents=True, exist_ok=True)
    (bundle / "fixtures" / "youcom" / "articles.json").write_text("[]", encoding="utf-8")
    (bundle / "fixtures" / "tavily" / "articles.json").write_text("[]", encoding="utf-8")
    (bundle / "leads" / "youcom_verified.json").write_text("[]", encoding="utf-8")
    (bundle / "leads" / "tavily_confirmed.json").write_text("[]", encoding="utf-8")
    captured_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    manifest = {
        "schema_version": 1,
        "bundle_id": "bundle-sample",
        "captured_at": captured_at,
        "expiry_days": 7,
        "git_commit": None,
        "tool_version": "1.0.0",
        "providers": [],
        "files": [],
        "signature": None,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    latest = bundle.parent / "latest.json"
    latest.write_text(json.dumps({"bundle_prefix": str(bundle)}), encoding="utf-8")
    return bundle.parent


@pytest.fixture(autouse=True)
def reset_bundle_cache():
    fixture_loader.clear_bundle_cache()
    yield
    fixture_loader.clear_bundle_cache()


def test_ensure_bundle_success(monkeypatch, tmp_path: Path):
    root = create_bundle(tmp_path)
    monkeypatch.setenv(fixture_loader.FIXTURE_ROOT_ENV, str(root))
    bundle = fixture_loader.ensure_bundle(RuntimeMode.FIXTURE)
    assert bundle.bundle_id == "bundle-sample"
    assert bundle.fixtures_dir.exists()


def test_ensure_bundle_expired(monkeypatch, tmp_path: Path):
    root = create_bundle(tmp_path, age_days=10)
    monkeypatch.setenv(fixture_loader.FIXTURE_ROOT_ENV, str(root))
    with pytest.raises(fixture_loader.FixtureError) as excinfo:
        fixture_loader.ensure_bundle(RuntimeMode.FIXTURE)
    assert excinfo.value.code == "E_BUNDLE_EXPIRED"


def test_resolve_root_defaults_local(monkeypatch):
    monkeypatch.delenv(fixture_loader.FIXTURE_ROOT_ENV, raising=False)
    monkeypatch.setenv("FUND_SIGNAL_MODE", "fixture")
    monkeypatch.setenv("FUND_SIGNAL_SOURCE", "local")
    config = get_runtime_config()
    root = fixture_loader.resolve_fixture_root(config)
    assert root == Path("fixtures/sample")


def test_resolve_root_defaults_supabase(monkeypatch):
    monkeypatch.delenv(fixture_loader.FIXTURE_ROOT_ENV, raising=False)
    monkeypatch.setenv("FUND_SIGNAL_MODE", "fixture")
    monkeypatch.setenv("FUND_SIGNAL_SOURCE", "supabase")
    config = get_runtime_config()
    root = fixture_loader.resolve_fixture_root(config)
    assert root == Path("fixtures/latest")


def test_build_freshness_metadata_allows_injected_clock():
    captured_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    fake_now = captured_at + timedelta(days=2)

    metadata = build_freshness_metadata("bundle-sample", captured_at, expiry_days=10, now=fake_now)

    assert metadata.expires_in_days == pytest.approx(8)
    assert metadata.warning is False
    assert "Expires in 8 days" in metadata.watermark


def test_build_freshness_metadata_sets_warning_near_expiry():
    captured_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    threshold_days = 10 * WARNING_THRESHOLD
    fake_now = captured_at + timedelta(days=threshold_days)

    metadata = build_freshness_metadata("bundle-sample", captured_at, expiry_days=10, now=fake_now)

    assert metadata.warning is True
    assert metadata.age_days == pytest.approx(threshold_days)


def test_resolve_bundle_context_noop_when_not_fixture(tmp_path):
    config = RuntimeConfig(mode=RuntimeMode.ONLINE, source=FixtureSource.LOCAL)
    spec = fixture_loader.FixtureArtifactSpec(default_path=Path("input.json"), location="leads_dir")

    context = fixture_loader.resolve_bundle_context(
        config,
        input_path=Path("custom.json"),
        output_path=Path("out.json"),
        input_spec=spec,
    )

    assert context.bundle is None
    assert context.input_path == Path("custom.json")
    assert context.output_path == Path("out.json")


def test_resolve_bundle_context_swaps_defaults(monkeypatch, tmp_path):
    root = create_bundle(tmp_path)
    monkeypatch.setenv(fixture_loader.FIXTURE_ROOT_ENV, str(root))
    config = RuntimeConfig(mode=RuntimeMode.FIXTURE, source=FixtureSource.LOCAL)
    input_default = Path("leads/youcom_verified.json")
    spec = fixture_loader.FixtureArtifactSpec(default_path=input_default, location="leads_dir")

    context = fixture_loader.resolve_bundle_context(
        config,
        input_path=input_default,
        output_path=Path("custom.json"),
        input_spec=spec,
    )

    assert context.bundle is not None
    assert context.input_path == context.bundle.leads_dir / "youcom_verified.json"
    assert context.output_path == Path("custom.json")
