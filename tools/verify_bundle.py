"""Verify bundle manifest checksums, freshness, and signature."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("tools.verify_bundle")

HASH_CHUNK_SIZE = 64 * 1024


class VerificationError(RuntimeError):
    """Raised when a manifest fails validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ManifestFile:
    """Normalized view of a manifest file entry."""

    path: str
    checksum: str

    @classmethod
    def from_dict(cls, data: dict) -> ManifestFile:
        path = data.get("path")
        checksum = data.get("checksum")
        if not path or not checksum:
            raise VerificationError("E_MANIFEST_INVALID", "File entry missing path or checksum.")
        return cls(path=path, checksum=checksum)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify bundle manifest integrity and freshness.")
    parser.add_argument("--manifest", type=Path, required=True, help="Path to manifest.json.")
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as infile:
        for chunk in iter(lambda: infile.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def verify_freshness(manifest: dict) -> None:
    captured_at = manifest.get("captured_at")
    expiry_days = manifest.get("expiry_days")
    if not captured_at or expiry_days is None:
        raise VerificationError("E_MANIFEST_INVALID", "Manifest missing captured_at or expiry_days.")
    captured_dt = parse_timestamp(captured_at)
    now = datetime.now(UTC)
    age = now - captured_dt
    age_days = age.total_seconds() / 86400
    logger.info("Bundle age: %.2f days (expiry %s days)", age_days, expiry_days)
    if age > timedelta(days=expiry_days):
        raise VerificationError("E_BUNDLE_EXPIRED", f"Bundle expired: age {age_days:.2f}d exceeds {expiry_days}d.")


def verify_checksums(bundle_dir: Path, manifest: dict) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise VerificationError("E_MANIFEST_INVALID", "Manifest missing files section.")
    for file_entry in _manifest_files(files):
        file_path = bundle_dir / file_entry.path
        if not file_path.exists():
            raise VerificationError("E_FILE_MISSING", f"Missing file referenced in manifest: {file_entry.path}")
        actual = sha256_file(file_path)
        if actual != file_entry.checksum:
            raise VerificationError("E_CHECKSUM_MISMATCH", f"Checksum mismatch for {file_entry.path}")


def _manifest_files(entries: Iterable[dict]) -> Iterable[ManifestFile]:
    for entry in entries:
        if not isinstance(entry, dict):
            raise VerificationError("E_MANIFEST_INVALID", "Invalid file entry in manifest.")
        yield ManifestFile.from_dict(entry)


def verify_signature(manifest: dict) -> None:
    signature = manifest.get("signature")
    if not signature:
        logger.info("Manifest has no signature; skipping signature verification.")
        return
    key = os.getenv("BUNDLE_HMAC_KEY")
    if not key:
        raise VerificationError("E_SIGNATURE_KEY_REQUIRED", "Signature present but BUNDLE_HMAC_KEY unset.")
    payload = _canonical_json(manifest)
    actual = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if actual != signature:
        raise VerificationError("E_SIGNATURE_MISMATCH", "Manifest signature mismatch.")


def verify_manifest(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_dir = manifest_path.parent
    verify_freshness(manifest)
    verify_checksums(bundle_dir, manifest)
    verify_signature(manifest)
    logger.info("Manifest verified successfully for %s.", bundle_dir)


def _canonical_json(manifest: dict) -> bytes:
    manifest_no_sig = dict(manifest)
    manifest_no_sig.pop("signature", None)
    return json.dumps(manifest_no_sig, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        verify_manifest(args.manifest.resolve())
    except VerificationError as exc:
        logger.error("Verification failed (%s): %s", exc.code, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
