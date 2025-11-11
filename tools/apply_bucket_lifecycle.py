"""Generate storage lifecycle policies for Supabase/S3 buckets."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("tools.apply_bucket_lifecycle")

DEFAULT_RAW_RETENTION_DAYS = 30
DEFAULT_CANONICAL_RETENTION_DAYS = 90


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit bucket lifecycle rules that match retention policy.")
    parser.add_argument("--bucket", required=True, help="Bucket name (Supabase or S3).")
    parser.add_argument(
        "--raw-days",
        type=_positive_days,
        default=DEFAULT_RAW_RETENTION_DAYS,
        help=f"Retention window for raw payloads (default {DEFAULT_RAW_RETENTION_DAYS}).",
    )
    parser.add_argument(
        "--canonical-days",
        type=_positive_days,
        default=DEFAULT_CANONICAL_RETENTION_DAYS,
        help=f"Retention window for canonical artifacts (default {DEFAULT_CANONICAL_RETENTION_DAYS}).",
    )
    parser.add_argument("--output", type=Path, default=Path("bucket-lifecycle.json"), help="Where to write lifecycle JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Log policy without writing a file.")
    return parser.parse_args(argv)


def build_policy(bucket: str, *, raw_days: int, canonical_days: int) -> dict:
    return {
        "bucket": bucket,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rules": [
            {
                "id": "raw-retention",
                "prefix": "raw/",
                "expiration_days": raw_days,
                "description": "Delete raw vendor payloads (jsonl.gz) after SLA window.",
            },
            {
                "id": "canonical-retention",
                "prefix": "",
                "expiration_days": canonical_days,
                "description": "Delete canonical JSON + manifests after 90 days.",
            },
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    policy = build_policy(args.bucket, raw_days=args.raw_days, canonical_days=args.canonical_days)

    logger.info(
        "Lifecycle policy bucket=%s raw=%sd canonical=%sd",
        args.bucket,
        args.raw_days,
        args.canonical_days,
    )
    if not args.dry_run:
        args.output.write_text(json.dumps(policy, indent=2), encoding="utf-8")
        logger.info("Wrote policy to %s", args.output)
    else:
        logger.info("[DRY-RUN] Policy not persisted.")
    return 0


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer day count.") from exc
    if days <= 0:
        raise argparse.ArgumentTypeError("Retention windows must be greater than zero days.")
    return days


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
