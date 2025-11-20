"""Cron-friendly entrypoint to render and send the Day-3 email digest."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from pipelines.day3 import DEFAULT_COMPANY_LIMIT, DeliveryError, resolve_scoring_run
from pipelines.day3 import email_delivery

logger = logging.getLogger("pipelines.day3.email_schedule")

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_MIN_SCORE = 80


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Schedule Day-3 email digests (cron entrypoint).")
    parser.add_argument(
        "--scoring-run",
        type=str,
        default=None,
        help="Scoring run identifier (defaults to DELIVERY_SCORING_RUN).",
    )
    parser.add_argument(
        "--company-limit",
        type=int,
        default=DEFAULT_COMPANY_LIMIT,
        help="Maximum number of companies to include (default: 25).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=DEFAULT_MIN_SCORE,
        help="Filter out companies with scores below this threshold (default: 80 for VERIFIED band).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=email_delivery.DEFAULT_OUTPUT,
        help="Output stem for digest artifacts (Markdown/HTML/CSV).",
    )
    parser.add_argument(
        "--deliver",
        action=argparse.BooleanOptionalAction,
        default=settings.delivery_email_force_run,
        help="Send via SMTP after rendering (use --no-deliver to disable).",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default=DEFAULT_TIMEZONE,
        help="Timezone used for scheduling/logging (default: America/Los_Angeles).",
    )
    parser.add_argument(
        "--enforce-window",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="If set, require current time to be Monday 09:00 in the configured timezone.",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="Override the current time (ISO8601) for testing or backfills.",
    )
    return parser.parse_args(argv)


def _parse_current_time(now_override: str | None, tz_name: str) -> datetime:
    tzinfo = ZoneInfo(tz_name)
    if now_override:
        return datetime.fromisoformat(now_override).astimezone(tzinfo)
    return datetime.now(tzinfo)


def _enforce_schedule_window(current_time: datetime) -> None:
    is_monday = current_time.weekday() == 0
    is_nine_am = current_time.hour == 9
    if not (is_monday and is_nine_am):
        raise DeliveryError(
            "Scheduled email delivery must run Monday at 09:00 in the configured timezone.",
            code="E_SCHEDULE_WINDOW",
        )


def run(argv: list[str] | None = None) -> Path:
    args = parse_args(argv)
    scoring_run_id = resolve_scoring_run(args.scoring_run)
    current_time = _parse_current_time(args.now, args.timezone)
    if args.enforce_window:
        _enforce_schedule_window(current_time)
    logger.info(
        "delivery.email.schedule.start",
        extra={
            "scoring_run_id": scoring_run_id,
            "timezone": args.timezone,
            "timestamp": current_time.isoformat(),
            "company_limit": args.company_limit,
            "min_score": args.min_score,
            "deliver": args.deliver,
            "output": str(args.output),
        },
    )
    email_args: list[str] = [
        "--scoring-run",
        scoring_run_id,
        "--output",
        str(args.output),
        "--company-limit",
        str(args.company_limit),
        "--min-score",
        str(args.min_score),
    ]
    email_args.append("--deliver" if args.deliver else "--no-deliver")
    result = email_delivery.run(email_args)
    logger.info(
        "delivery.email.schedule.success",
        extra={
            "scoring_run_id": scoring_run_id,
            "timezone": args.timezone,
            "timestamp": current_time.isoformat(),
            "output": str(result),
        },
    )
    return result


def main() -> None:
    try:
        run()
    except DeliveryError as exc:
        logger.error(
            "delivery.email.schedule.failed",
            extra={"code": exc.code, "error": str(exc)},
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
