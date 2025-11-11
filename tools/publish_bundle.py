"""Upload a capture bundle to Supabase storage and update latest pointer."""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tools import verify_bundle
from tools.verify_bundle import VerificationError

logger = logging.getLogger("tools.publish_bundle")


class PublishError(RuntimeError):
    """Raised when publishing fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SupabaseTarget:
    base_url: str
    service_key: str
    bucket: str

    def object_url(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.base_url.rstrip('/')}/storage/v1/object/{self.bucket}/{key}"

    def headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "x-upsert": "true",
            "Content-Type": "application/octet-stream",
        }


@dataclass(frozen=True)
class BundleMetadata:
    """Resolved manifest payload."""

    bundle_dir: Path
    manifest_path: Path
    data: dict

    @property
    def bundle_id(self) -> str:
        return self.data.get("bundle_id", self.bundle_dir.name)

    @property
    def manifest_bytes(self) -> bytes:
        return json.dumps(self.data, indent=2).encode("utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload bundle files to Supabase and update latest pointer.")
    parser.add_argument("--bundle", type=Path, required=True, help="Local bundle directory (artifacts/.../bundle-...).")
    parser.add_argument("--remote-prefix", required=True, help="Remote prefix (e.g., artifacts/2025/11/07/bundle-...).")
    parser.add_argument("--pointer-path", default="artifacts/latest.json", help="Remote path for latest.json pointer.")
    parser.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"), help="Supabase base URL.")
    parser.add_argument("--supabase-service-key", default=os.getenv("SUPABASE_SERVICE_KEY"), help="Supabase service key.")
    parser.add_argument("--bucket", default=os.getenv("SUPABASE_BUCKET", "fundsignal-artifacts"), help="Supabase bucket name.")
    return parser.parse_args(argv)


def init_target(args: argparse.Namespace) -> SupabaseTarget:
    if not args.supabase_url or not args.supabase_service_key:
        raise PublishError("E_PUBLISH_AUTH", "Supabase credentials missing (SUPABASE_URL / SUPABASE_SERVICE_KEY).")
    return SupabaseTarget(
        base_url=args.supabase_url,
        service_key=args.supabase_service_key,
        bucket=args.bucket,
    )


def publish(args: argparse.Namespace) -> None:
    metadata = load_bundle(args.bundle)
    remote_prefix = normalize_remote_prefix(args.remote_prefix)
    pointer_path = args.pointer_path.lstrip("/")
    target = init_target(args)

    logger.info("Uploading bundle %s to %s/%s", metadata.bundle_id, target.bucket, remote_prefix)
    with httpx.Client(timeout=httpx.Timeout(None)) as client:
        upload_bundle_files(client, target, metadata.bundle_dir, remote_prefix)
        pointer_payload = build_pointer_payload(metadata.bundle_id, remote_prefix)
        upload_bytes(client, target, pointer_path, pointer_payload)

    logger.info("Published bundle %s and updated pointer %s.", metadata.bundle_id, pointer_path)


def iter_bundle_files(bundle_dir: Path) -> Iterable[Path]:
    for file_path in sorted(bundle_dir.rglob("*")):
        if file_path.is_file():
            yield file_path


def load_bundle(bundle_dir: Path | str) -> BundleMetadata:
    bundle_path = Path(bundle_dir).resolve()
    if not bundle_path.exists():
        raise PublishError("E_PUBLISH_BUNDLE", f"Bundle directory not found: {bundle_path}")
    manifest_path = bundle_path / "manifest.json"
    try:
        verify_bundle.verify_manifest(manifest_path)
    except VerificationError as exc:
        raise PublishError(exc.code, f"Bundle verification failed prior to upload: {exc}") from exc
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return BundleMetadata(bundle_dir=bundle_path, manifest_path=manifest_path, data=data)


def normalize_remote_prefix(prefix: str) -> str:
    cleaned = prefix.strip().strip("/")
    if not cleaned:
        raise PublishError("E_PUBLISH_PREFIX", "Remote prefix cannot be empty.")
    return cleaned


def upload_bundle_files(client: httpx.Client, target: SupabaseTarget, bundle_dir: Path, remote_prefix: str) -> None:
    for file_path in iter_bundle_files(bundle_dir):
        rel = file_path.relative_to(bundle_dir).as_posix()
        upload_bytes(client, target, f"{remote_prefix}/{rel}", file_path.read_bytes())


def build_pointer_payload(bundle_id: str, remote_prefix: str) -> bytes:
    payload = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "bundle_prefix": remote_prefix,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def upload_bytes(client: httpx.Client, target: SupabaseTarget, key: str, data: bytes) -> None:
    url = target.object_url(key)
    response = client.post(url, headers=target.headers(), content=data)
    if response.status_code in (401, 403):
        raise PublishError("E_PUBLISH_AUTH", "Supabase authentication failed while uploading bundle.")
    response.raise_for_status()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        publish(args)
    except PublishError as exc:
        logger.error("Publish failed (%s): %s", exc.code, exc)
        return 1
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unexpected publish failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
