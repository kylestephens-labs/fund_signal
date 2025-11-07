"""Sync latest fixtures into the local sandbox."""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path
from typing import Sequence

from tools import promote_latest

logger = logging.getLogger("tools.sync_fixtures")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync fixture bundle into ./fixtures/latest.")
    parser.add_argument("--source", type=Path, default=Path("fixtures/sample/bundle-sample"), help="Bundle directory to copy from.")
    parser.add_argument("--dest-root", type=Path, default=Path("fixtures/latest"), help="Root directory for synced bundles.")
    return parser.parse_args(argv)


def copy_bundle(source: Path, dest_root: Path) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"Source bundle not found: {source}")
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_bundle = dest_root / source.name
    if dest_bundle.exists():
        shutil.rmtree(dest_bundle)
    shutil.copytree(source, dest_bundle)
    return dest_bundle


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        dest_bundle = copy_bundle(args.source.resolve(), args.dest_root.resolve())
        latest_path = args.dest_root / "latest.json"
        promote_latest.promote(dest_bundle, latest_path)
        logger.info("Synced bundle %s to %s.", dest_bundle.name, args.dest_root)
    except Exception as exc:  # pragma: no cover - safety net
        logger.error("Sync failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

