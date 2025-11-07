"""Promote a completed capture bundle by updating latest.json atomically."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger("tools.promote_latest")

REQUIRED_FILES: tuple[Path, ...] = (
    Path("manifest.json"),
    Path("fixtures/youcom/articles.json"),
    Path("fixtures/tavily/articles.json"),
    Path("leads/youcom_verified.json"),
    Path("leads/tavily_confirmed.json"),
)


@dataclass(frozen=True)
class LatestPayload:
    """Shape of the latest.json document."""

    schema_version: int
    bundle_id: str
    bundle_prefix: str
    generated_at: str
    manifest: dict
    files: list[dict]

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "bundle_id": self.bundle_id,
                "bundle_prefix": self.bundle_prefix,
                "generated_at": self.generated_at,
                "manifest": self.manifest,
                "files": self.files,
            },
            indent=2,
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote a capture bundle by updating latest.json.")
    parser.add_argument("--prefix", type=Path, required=True, help="Path to the bundle directory.")
    parser.add_argument(
        "--latest-path",
        type=Path,
        help="Path to write latest.json. Defaults to bundle root's parent/latest.json.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print pending changes without writing.")
    return parser.parse_args(argv)


def validate_bundle(bundle_dir: Path) -> None:
    if not bundle_dir.exists():
        raise FileNotFoundError(f"Bundle directory not found: {bundle_dir}")
    missing = [bundle_dir / rel for rel in REQUIRED_FILES if not (bundle_dir / rel).exists()]
    if missing:
        missing_list = "\n  ".join(str(path) for path in missing)
        raise FileNotFoundError(f"Bundle is incomplete; missing critical files:\n  {missing_list}")


def gather_file_metadata(bundle_dir: Path) -> list[dict]:
    files: list[dict] = []
    for file_path in _iter_files(bundle_dir):
        rel = file_path.relative_to(bundle_dir).as_posix()
        size = file_path.stat().st_size
        files.append({"path": rel, "size": size})
    return files


def _iter_files(root: Path) -> Iterable[Path]:
    """Yield file paths under root in deterministic order."""
    return (
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def atomic_write(path: Path, payload: str) -> None:
    temp_path = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(path)


def promote(bundle_dir: Path, latest_path: Path, dry_run: bool = False) -> None:
    validate_bundle(bundle_dir)
    payload = build_payload(bundle_dir)
    serialized = payload.to_json()

    if dry_run:
        logger.info("Dry run: would write %s with payload:\n%s", latest_path, serialized)
        return

    atomic_write(latest_path, serialized)
    logger.info(
        "Promoted bundle %s -> %s (%s files)",
        payload.bundle_id,
        latest_path,
        len(payload.files),
    )


def build_payload(bundle_dir: Path) -> LatestPayload:
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return LatestPayload(
        schema_version=1,
        bundle_id=bundle_dir.name,
        bundle_prefix=bundle_dir.as_posix(),
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        manifest=manifest,
        files=gather_file_metadata(bundle_dir),
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    bundle_dir = args.prefix.resolve()
    latest_path = (args.latest_path or bundle_dir.parent / "latest.json").resolve()
    try:
        promote(bundle_dir, latest_path, dry_run=args.dry_run)
    except Exception as exc:  # pragma: no cover - shell invocation safety
        logger.error("Promotion failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
