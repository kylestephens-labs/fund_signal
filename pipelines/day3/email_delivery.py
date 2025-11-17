"""Render Day-3 email digests from persisted CompanyScore rows."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from app.config import settings
from app.models.company import CompanyScore
from pipelines.day3 import (
    DEFAULT_COMPANY_LIMIT,
    DeliveryError,
    compute_confidence,
    fetch_scores_for_delivery,
    record_delivery_event,
    resolve_limit,
    resolve_scoring_run,
    summarize_proofs,
    utc_now,
)

logger = logging.getLogger("pipelines.day3.email")

DEFAULT_OUTPUT = Path(settings.delivery_output_dir or "output") / "email_delivery.md"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render FundSignal Day-3 email digests.")
    parser.add_argument(
        "--scoring-run",
        type=str,
        default=None,
        help="Scoring run identifier (defaults to DELIVERY_SCORING_RUN).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Destination for the rendered Markdown (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of companies to include.",
    )
    parser.add_argument(
        "--company-limit",
        type=int,
        default=None,
        help="Alias for --limit to match CLI docs.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        default=settings.delivery_force_refresh,
        help="Optional flag recorded in logs when forcing a re-score upstream.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=60,
        help="Filter out companies with scores below this threshold (default: 60).",
    )
    return parser.parse_args(argv)


def render_email(scoring_run_id: str, scores: Sequence[CompanyScore]) -> str:
    """Return a Markdown digest for the supplied scoring run."""
    timestamp = utc_now()
    lines: list[str] = [
        f"# FundSignal Delivery — Run {scoring_run_id}",
        "",
        f"_Generated at {timestamp}_",
        "",
    ]
    for index, score in enumerate(scores, start=1):
        confidence = compute_confidence(score.score)
        company_label = str(score.company_id)
        lines.append(f"## {index}. {company_label} — {score.score} pts ({confidence})")
        lines.append("")
        lines.append(f"- **Recommended approach:** {score.recommended_approach}")
        lines.append(f"- **Pitch angle:** {score.pitch_angle}")
        lines.append("")
        lines.append("### Why this score")
        for entry in score.breakdown:
            lines.append(f"- **{entry.reason}** — {entry.points} pts")
            proofs = summarize_proofs(entry)
            if proofs:
                for proof in proofs:
                    url = proof["source_url"]
                    verifiers = ", ".join(proof["verified_by"] or []) if proof["verified_by"] else "FundSignal"
                    lines.append(f"  - [{url}]({url}) _(verified by {verifiers})_")
            else:
                lines.append("  - _(No proofs available; flagged for follow-up)_")
        lines.append("")
    if not scores:
        lines.append("No companies qualified for this scoring run.")
    return "\n".join(lines).strip() + "\n"


def run(argv: Sequence[str] | None = None) -> Path:
    args = parse_args(argv)
    scoring_run_id = resolve_scoring_run(args.scoring_run)
    limit_arg = args.company_limit if args.company_limit is not None else args.limit
    limit = resolve_limit(limit_arg, default=DEFAULT_COMPANY_LIMIT)
    scores = fetch_scores_for_delivery(scoring_run_id, limit=limit)
    filtered = [score for score in scores if score.score >= args.min_score]
    if not filtered:
        raise DeliveryError(
            f"All companies fell below the minimum score of {args.min_score}.",
            code="E_NO_COMPANIES",
        )
    if args.force_refresh:
        logger.info(
            "delivery.email.force_refresh",
            extra={"scoring_run_id": scoring_run_id},
        )
    payload = render_email(scoring_run_id, filtered)
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload, encoding="utf-8")
    record_delivery_event(
        "email",
        scoring_run_id=scoring_run_id,
        count=len(filtered),
        output_path=str(output_path),
    )
    logger.info(
        "delivery.email.rendered",
        extra={"scoring_run_id": scoring_run_id, "output": str(output_path)},
    )
    return output_path


def main() -> None:
    """Entry point for `python -m pipelines.day3.email_delivery`."""
    try:
        run()
    except DeliveryError as exc:
        logger.error("delivery.email.failed", extra={"code": exc.code, "error": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
