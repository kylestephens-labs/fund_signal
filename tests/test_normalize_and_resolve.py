import json
from pathlib import Path

from tools import normalize_and_resolve


def _write_raw(tmp_path: Path) -> Path:
    raw_rows = [
        {
            "id": "row_001",
            "title": "The SaaS News | Appy.ai raises $10M Series A",
            "url": "https://www.thesaasnews.com/news/appy.ai-raises-10m",
            "funding_stage": "Series A",
            "funding_amount": 10_000_000,
            "funding_currency": "USD",
            "funding_date": "2025-10-01",
        }
    ]
    leads_dir = tmp_path / "leads"
    leads_dir.mkdir(parents=True, exist_ok=True)
    input_path = leads_dir / "exa_seed.json"
    input_path.write_text(json.dumps(raw_rows), encoding="utf-8")
    return input_path


def test_normalize_and_resolve_end_to_end(tmp_path: Path):
    input_path = _write_raw(tmp_path)
    candidates_out = tmp_path / "leads" / "exa_seed.candidates.json"
    normalized_out = tmp_path / "leads" / "exa_seed.normalized.json"

    summary = normalize_and_resolve.run_pipeline(
        input_path=input_path,
        candidates_out=candidates_out,
        normalized_out=normalized_out,
        normalizer_rules=Path("configs/normalizer_rules.v1.yaml"),
        resolver_rules=Path("configs/resolver_rules.v1.yaml"),
    )

    assert candidates_out.exists()
    assert normalized_out.exists()
    normalized_payload = json.loads(normalized_out.read_text(encoding="utf-8"))
    entry = normalized_payload["data"][0]
    assert entry["company_name"]
    assert entry["funding_stage"] == "Series A"
    assert entry["amount"]["unit"] in {"M", "K", "B"}
    assert entry["source_url"]
    # Determinism: re-run and compare bytes
    first_candidates = candidates_out.read_bytes()
    first_normalized = normalized_out.read_bytes()
    normalize_and_resolve.run_pipeline(
        input_path=input_path,
        candidates_out=candidates_out,
        normalized_out=normalized_out,
        normalizer_rules=Path("configs/normalizer_rules.v1.yaml"),
        resolver_rules=Path("configs/resolver_rules.v1.yaml"),
    )
    assert first_candidates == candidates_out.read_bytes()
    assert first_normalized == normalized_out.read_bytes()
    assert summary["generator"]["ruleset_version"]
    assert summary["resolver"]["ruleset_version"]


def test_normalize_and_resolve_updates_manifest(tmp_path: Path, monkeypatch):
    input_path = _write_raw(tmp_path)
    bundle_dir = input_path.parent.parent
    manifest_path = bundle_dir / "manifest.json"
    manifest = {
        "bundle_id": "bundle-test",
        "captured_at": "2025-11-10T00:00:00Z",
        "expiry_days": 7,
        "files": [{"path": "leads/exa_seed.json", "sha256": "placeholder"}],
        "signature": None,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    candidates_out = bundle_dir / "leads" / "exa_seed.candidates.json"
    normalized_out = bundle_dir / "leads" / "exa_seed.normalized.json"

    normalize_and_resolve.run_pipeline(
        input_path=input_path,
        candidates_out=candidates_out,
        normalized_out=normalized_out,
        normalizer_rules=Path("configs/normalizer_rules.v1.yaml"),
        resolver_rules=Path("configs/resolver_rules.v1.yaml"),
        manifest_path=manifest_path,
    )

    updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = {entry["path"]: entry["sha256"] for entry in updated_manifest["files"]}
    assert "leads/exa_seed.candidates.json" in paths
    assert "leads/exa_seed.normalized.json" in paths
