"""Helpers for updating bundle manifests with file hashes."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path

from tools import capture_pipeline

logger = logging.getLogger("tools.manifest_utils")


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as infile:
        for chunk in iter(lambda: infile.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest missing at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def update_manifest(manifest_path: Path, files: Mapping[str, str]) -> None:
    manifest = load_manifest(manifest_path)
    manifest_files = manifest.setdefault("files", [])

    for rel_path, sha in files.items():
        entry = next((item for item in manifest_files if item.get("path") == rel_path), None)
        if entry:
            entry["sha256"] = sha
        else:
            manifest_files.append({"path": rel_path, "sha256": sha})

    manifest_no_sig = dict(manifest)
    manifest_no_sig.pop("signature", None)
    manifest["signature"] = capture_pipeline.sign_manifest(manifest_no_sig)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Updated manifest %s for %s", manifest_path, list(files.keys()))
