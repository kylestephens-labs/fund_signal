"""Generate Slack-ready payloads for persisted scoring runs."""

from __future__ import annotations

import argparse
import json
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
    serialize_score,
    summarize_proofs,
    utc_now,
)

logger = logging.getLogger("pipelines.day3.slack")

DEFAULT_OUTPUT = Path(settings.delivery_output_dir or "output") / "slack_delivery.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Slack payloads for FundSignal Day-3 delivery.")
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
        help=f"Destination for the Slack payload JSON (default: {DEFAULT_OUTPUT})",
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
        help="Alias for --limit (matches runbook terminology).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=60,
        help="Filter out companies with scores below this threshold (default: 60).",
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=settings.slack_webhook_url,
        help="Optional Slack webhook recorded in the payload metadata.",
    )
    return parser.parse_args(argv)


def build_slack_payload(scoring_run_id: str, scores: Sequence[CompanyScore], *, webhook_url: str | None) -> dict:
    """Return a Slack-compatible payload with per-company breakdowns."""
    timestamp = utc_now()
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*FundSignal Run {scoring_run_id}*\nGenerated {timestamp} UTC",
            },
        },
        {"type": "divider"},
    ]
    for index, score in enumerate(scores, start=1):
        confidence = compute_confidence(score.score)
        proof_lines = _render_proof_lines(score)
        text_lines = [
            f"*{index}. {score.company_id} — {score.score} pts ({confidence})*",
            f"*Approach:* {score.recommended_approach}",
            f"*Pitch:* {score.pitch_angle}",
        ]
        if proof_lines:
            text_lines.append("*Proofs:*")
            text_lines.extend(proof_lines)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(text_lines),
                },
            }
        )
        blocks.append({"type": "divider"})
    if blocks and blocks[-1].get("type") == "divider":
        blocks.pop()
    return {
        "text": f"FundSignal run {scoring_run_id} ready with {len(scores)} companies.",
        "blocks": blocks,
        "metadata": {
            "scoring_run_id": scoring_run_id,
            "generated_at": timestamp,
            "company_count": len(scores),
            "webhook_url": webhook_url,
            "scores": [serialize_score(score) for score in scores],
        },
    }


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
    payload = build_slack_payload(scoring_run_id, filtered, webhook_url=args.webhook_url)
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    record_delivery_event(
        "slack",
        scoring_run_id=scoring_run_id,
        count=len(filtered),
        output_path=str(output_path),
    )
    logger.info(
        "delivery.slack.rendered",
        extra={"scoring_run_id": scoring_run_id, "output": str(output_path)},
    )
    return output_path


def _render_proof_lines(score: CompanyScore, max_items: int = 2) -> list[str]:
    """Render the top proof links for Slack markdown."""
    proof_lines: list[str] = []
    for entry in score.breakdown:
        for proof in summarize_proofs(entry):
            if len(proof_lines) >= max_items:
                break
            url = proof["source_url"]
            verifiers = ", ".join(proof["verified_by"] or []) if proof["verified_by"] else ""
            proof_lines.append(f"• <{url}|{entry.reason}> {f'({verifiers})' if verifiers else ''}".rstrip())
        if len(proof_lines) >= max_items:
            break
    return proof_lines


def main() -> None:
    """Entry point for `python -m pipelines.day3.slack_delivery`."""
    try:
        run()
    except DeliveryError as exc:
        logger.error("delivery.slack.failed", extra={"code": exc.code, "error": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
