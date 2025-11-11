"""Sync the latest verified fixture bundle into the sandbox."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from tools import promote_latest, verify_bundle
from tools.verify_bundle import VerificationError

logger = logging.getLogger("tools.sync_fixtures")

SourceType = Literal["supabase", "local"]

SUPABASE_URL_ENV = "SUPABASE_URL"
SUPABASE_SERVICE_KEY_ENV = "SUPABASE_SERVICE_KEY"
SUPABASE_BUCKET_ENV = "SUPABASE_BUCKET"


class SyncError(RuntimeError):
    """Raised when sync cannot complete."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LatestPointer:
    bundle_id: str
    bundle_prefix: str

    @classmethod
    def from_dict(cls, payload: dict) -> LatestPointer:
        bundle_id = payload.get("bundle_id")
        bundle_prefix = payload.get("bundle_prefix")
        if not bundle_prefix:
            raise SyncError("E_SYNC_POINTER", "latest.json missing bundle_prefix.")
        if not bundle_id:
            bundle_id = Path(bundle_prefix).name
        return cls(bundle_id=bundle_id, bundle_prefix=bundle_prefix)


@dataclass
class SupabaseConfig:
    base_url: str
    service_key: str
    bucket: str
    prefix: str
    pointer_path: str


@dataclass
class BundleInfo:
    bundle_id: str
    bundle_dir: Path
    manifest: dict

    @property
    def captured_at(self) -> str | None:
        return self.manifest.get("captured_at")

    @property
    def expiry_days(self) -> int | None:
        return self.manifest.get("expiry_days")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download, verify, and install the latest fixture bundle.")
    parser.add_argument("--source", choices=["supabase", "local"], default="supabase", help="Fixture source.")
    parser.add_argument("--dest", type=Path, default=Path("fixtures/latest"), help="Destination root.")
    parser.add_argument(
        "--local-root",
        type=Path,
        default=Path("fixtures/sample"),
        help="Root containing latest.json for local source.",
    )
    parser.add_argument("--supabase-prefix", default="artifacts", help="Supabase prefix where bundles are stored.")
    parser.add_argument(
        "--supabase-bucket",
        default=os.getenv(SUPABASE_BUCKET_ENV, "fundsignal-artifacts"),
        help="Supabase bucket name.",
    )
    parser.add_argument("--pointer", default="latest.json", help="Pointer filename relative to prefix.")
    return parser.parse_args(argv)


def ensure_supabase_config(args: argparse.Namespace) -> SupabaseConfig:
    base_url = os.getenv(SUPABASE_URL_ENV)
    service_key = os.getenv(SUPABASE_SERVICE_KEY_ENV)
    if not base_url or not service_key:
        raise SyncError("E_SYNC_AUTH", "Supabase credentials missing (SUPABASE_URL / SUPABASE_SERVICE_KEY).")
    return SupabaseConfig(
        base_url=base_url.rstrip("/"),
        service_key=service_key,
        bucket=args.supabase_bucket,
        prefix=args.supabase_prefix.strip("/"),
        pointer_path=args.pointer.lstrip("/"),
    )


def supabase_headers(token: str) -> dict[str, str]:
    return {"apikey": token, "Authorization": f"Bearer {token}"}


def supabase_url(cfg: SupabaseConfig, relative_path: str) -> str:
    rel = relative_path.lstrip("/")
    return f"{cfg.base_url}/storage/v1/object/{cfg.bucket}/{rel}"


def download_supabase_json(cfg: SupabaseConfig, relative_path: str) -> dict:
    url = supabase_url(cfg, relative_path)
    try:
        response = httpx.get(url, headers=supabase_headers(cfg.service_key), timeout=30)
    except httpx.HTTPError as exc:  # pragma: no cover - network failure
        raise SyncError("E_SYNC_NETWORK", f"Supabase request failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise SyncError("E_SYNC_AUTH", "Supabase authentication failed (check service key permissions).")
    if response.status_code == 404:
        raise SyncError("E_SYNC_POINTER", f"Supabase object not found: {relative_path}")
    response.raise_for_status()
    return response.json()


def download_supabase_file(cfg: SupabaseConfig, relative_path: str, dest: Path) -> None:
    url = supabase_url(cfg, relative_path)
    headers = supabase_headers(cfg.service_key)
    try:
        with httpx.stream("GET", url, headers=headers, timeout=None) as stream:
            if stream.status_code in (401, 403):
                raise SyncError("E_SYNC_AUTH", "Supabase authentication failed (check service key permissions).")
            if stream.status_code == 404:
                raise SyncError("E_SYNC_MANIFEST", f"Missing bundle file: {relative_path}")
            stream.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as outfile:
                for chunk in stream.iter_bytes():
                    outfile.write(chunk)
    except httpx.HTTPError as exc:  # pragma: no cover - network failure
        raise SyncError("E_SYNC_NETWORK", f"Supabase download failed: {exc}") from exc


def resolve_local_pointer(root: Path) -> LatestPointer:
    latest = root / "latest.json"
    if not latest.exists():
        raise SyncError("E_SYNC_POINTER", f"latest.json not found at {latest}")
    payload = json.loads(latest.read_text(encoding="utf-8"))
    pointer = LatestPointer.from_dict(payload)
    prefix_path = Path(pointer.bundle_prefix)
    if not prefix_path.is_absolute():
        pointer = LatestPointer(
            bundle_id=pointer.bundle_id,
            bundle_prefix=str((root / prefix_path).resolve()),
        )
    return pointer


def sync_from_local(args: argparse.Namespace, dest_root: Path) -> BundleInfo:
    pointer = resolve_local_pointer(args.local_root)
    source_bundle = Path(pointer.bundle_prefix)
    if not source_bundle.exists():
        raise SyncError("E_SYNC_POINTER", f"Bundle not found at {source_bundle}")
    with staging_area(dest_root, pointer.bundle_id) as staging:
        shutil.copytree(source_bundle, staging, dirs_exist_ok=True)
        _verify_manifest(staging / "manifest.json")
        return install_bundle(staging, dest_root)


def sync_from_supabase(args: argparse.Namespace, dest_root: Path) -> BundleInfo:
    cfg = ensure_supabase_config(args)
    pointer_path = f"{cfg.prefix}/{cfg.pointer_path}".strip("/")
    pointer = LatestPointer.from_dict(download_supabase_json(cfg, pointer_path))
    bundle_prefix = pointer.bundle_prefix.strip("/")
    with staging_area(dest_root, pointer.bundle_id) as staging:
        manifest_rel = f"{bundle_prefix}/manifest.json"
        download_supabase_file(cfg, manifest_rel, staging / "manifest.json")
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        files = manifest.get("files")
        if not isinstance(files, list):
            raise SyncError("E_SYNC_MANIFEST", "Manifest missing files list.")
        for entry in files:
            rel_path = entry.get("path")
            if not isinstance(rel_path, str):
                raise SyncError("E_SYNC_MANIFEST", "Invalid file entry in manifest.")
            download_supabase_file(cfg, f"{bundle_prefix}/{rel_path}", staging / rel_path)
        _verify_manifest(staging / "manifest.json")
        return install_bundle(staging, dest_root)


@contextmanager
def staging_area(dest_root: Path, bundle_id: str):
    staging_root = dest_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / bundle_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        yield staging
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _verify_manifest(manifest_path: Path) -> dict:
    try:
        verify_bundle.verify_manifest(manifest_path)
    except VerificationError as exc:
        raise SyncError(exc.code, str(exc)) from exc
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def install_bundle(staging: Path, dest_root: Path) -> BundleInfo:
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    bundle_id = manifest["bundle_id"]
    final_dir = dest_root / bundle_id
    dest_root.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.move(staging, final_dir)
    latest_path = dest_root / "latest.json"
    promote_latest.promote(final_dir, latest_path)
    bundle = BundleInfo(bundle_id=bundle_id, bundle_dir=final_dir, manifest=manifest)
    log_bundle(bundle)
    return bundle


def sync(args: argparse.Namespace) -> BundleInfo:
    dest_root = args.dest.resolve()
    if args.source == "local":
        bundle = sync_from_local(args, dest_root)
    else:
        bundle = sync_from_supabase(args, dest_root)
    return bundle


def log_bundle(bundle: BundleInfo) -> None:
    captured_at = bundle.captured_at
    expiry_days = bundle.expiry_days
    logger.info(
        "Installed bundle %s (captured_at=%s, expiry_days=%s) at %s.",
        bundle.bundle_id,
        captured_at,
        expiry_days,
        bundle.bundle_dir,
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        sync(args)
    except SyncError as exc:
        logger.error("Sync failed (%s): %s", exc.code, exc)
        return 1
    except VerificationError as exc:
        logger.error("Verification failed during sync (%s): %s", exc.code, exc)
        return 1
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unexpected sync failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
