from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from tools import compress_raw_data


def _read_gzip_lines(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as infile:
        return [json.loads(line) for line in infile if line.strip()]


def _make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "raw").mkdir(parents=True)
    return bundle


def _write_payload(path: Path, records: list[dict]) -> None:
    if path.suffix == ".jsonl":
        serialized = "\n".join(json.dumps(record) for record in records) + "\n"
        path.write_text(serialized, encoding="utf-8")
    else:
        path.write_text(json.dumps(records), encoding="utf-8")


@pytest.mark.parametrize("filename", ["youcom.json", "exa.jsonl"])
def test_compress_bundle_transforms_raw_payload(tmp_path: Path, filename: str):
    bundle = _make_bundle(tmp_path)
    raw_path = bundle / "raw" / filename
    payload = [
        {"company": "Acme", "value": 1},
        {"company": "Beta", "value": 2},
    ]
    _write_payload(raw_path, payload)

    results = compress_raw_data.compress_bundle(bundle)

    assert not raw_path.exists()
    if raw_path.suffix == ".jsonl":
        output = raw_path.with_suffix(raw_path.suffix + ".gz")
    else:
        output = raw_path.with_suffix(".jsonl.gz")
    assert output.exists()
    assert results[0].records == len(payload)
    assert _read_gzip_lines(output) == payload


def test_compress_bundle_dry_run_does_not_modify_files(tmp_path: Path):
    bundle = _make_bundle(tmp_path)
    raw_path = bundle / "raw" / "exa.jsonl"
    _write_payload(raw_path, [{"company": "Acme"}])

    results = compress_raw_data.compress_bundle(bundle, dry_run=True)

    assert raw_path.exists()
    assert results[0].skipped is True


def test_find_raw_files_skips_precompressed_payloads(tmp_path: Path):
    bundle = _make_bundle(tmp_path)
    raw_dir = bundle / "raw"
    pending = raw_dir / "exa.json"
    pending.write_text("[]", encoding="utf-8")
    (raw_dir / "exa.jsonl.gz").write_text("", encoding="utf-8")
    (raw_dir / "notes.txt").write_text("", encoding="utf-8")

    assert compress_raw_data.find_raw_files(bundle) == [pending]
