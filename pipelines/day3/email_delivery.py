"""Render Day-3 email digests from persisted CompanyScore rows and optionally deliver via SMTP."""

from __future__ import annotations

import argparse
import html
import logging
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlparse

from app.config import settings
from app.models.company import CompanyScore
from app.observability.metrics import metrics
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
DEFAULT_SMTP_TIMEOUT = 15.0


@dataclass(frozen=True)
class SMTPDeliveryConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    use_ssl: bool
    disable_tls: bool
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    bcc_addresses: list[str]
    subject: str


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
    parser.add_argument(
        "--deliver",
        action=argparse.BooleanOptionalAction,
        default=settings.delivery_email_force_run,
        help="Send the rendered digest via SMTP after writing the artifact (use --no-deliver to skip).",
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
    if args.deliver:
        _deliver_via_smtp(scoring_run_id, payload, output_path)
    return output_path


def _deliver_via_smtp(scoring_run_id: str, payload: str, artifact_path: Path) -> None:
    config = _build_smtp_config(scoring_run_id)
    message, recipients = _render_email_message(config, payload, scoring_run_id)
    start = time.perf_counter()
    client = _create_smtp_client(config)
    try:
        if not config.use_ssl:
            client.ehlo()
            if not config.disable_tls:
                client.starttls()
                client.ehlo()
        if config.username:
            client.login(config.username, config.password or "")
        client.send_message(message, to_addrs=recipients)
    except Exception as exc:  # pragma: no cover - network guards
        logger.error(
            "delivery.email.error",
            extra={
                "scoring_run_id": scoring_run_id,
                "host": config.host,
                "port": config.port,
                "error": str(exc),
            },
        )
        raise DeliveryError(
            f"SMTP delivery failed for {config.host}:{config.port} — {exc}",
            code="E_SMTP_FAILED",
        ) from exc
    finally:
        try:
            client.quit()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("SMTP quit failed", exc_info=True)

    duration_ms = (time.perf_counter() - start) * 1000
    recipient_count = len(recipients)
    logger.info(
        "delivery.email.sent",
        extra={
            "scoring_run_id": scoring_run_id,
            "message_id": message["Message-ID"],
            "recipients": recipient_count,
            "output": str(artifact_path),
        },
    )
    metrics.increment(
        "delivery.email.sent",
        tags={"scoring_run": scoring_run_id, "recipient_count": recipient_count},
    )
    metrics.timing(
        "delivery.email.duration_ms",
        duration_ms,
        tags={"scoring_run": scoring_run_id},
    )


def _create_smtp_client(config: SMTPDeliveryConfig) -> smtplib.SMTP:
    if config.use_ssl:
        return smtplib.SMTP_SSL(config.host, config.port, timeout=DEFAULT_SMTP_TIMEOUT)
    return smtplib.SMTP(config.host, config.port, timeout=DEFAULT_SMTP_TIMEOUT)


def _render_email_message(
    config: SMTPDeliveryConfig, payload: str, scoring_run_id: str
) -> tuple[EmailMessage, list[str]]:
    message = EmailMessage()
    message_id = make_msgid()
    subject = config.subject or f"FundSignal Delivery — {scoring_run_id}"
    message["Subject"] = subject
    message["From"] = config.from_address
    recipients: list[str] = []
    if config.to_addresses:
        message["To"] = ", ".join(config.to_addresses)
        recipients.extend(config.to_addresses)
    if config.cc_addresses:
        message["Cc"] = ", ".join(config.cc_addresses)
        recipients.extend(config.cc_addresses)
    if config.bcc_addresses:
        recipients.extend(config.bcc_addresses)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id
    message.set_content(payload)
    message.add_alternative(f"<pre>{html.escape(payload)}</pre>", subtype="html")
    return message, recipients


def _build_smtp_config(scoring_run_id: str) -> SMTPDeliveryConfig:
    missing: list[str] = []
    if not settings.email_smtp_url:
        missing.append("EMAIL_SMTP_URL")
    if not settings.email_from:
        missing.append("EMAIL_FROM")
    recipients = _parse_recipient_list(settings.email_to)
    if not recipients:
        missing.append("EMAIL_TO")
    if missing:
        raise DeliveryError(
            f"--deliver requires the following env vars: {', '.join(missing)}.",
            code="E_SMTP_CONFIG",
        )
    parsed = urlparse(settings.email_smtp_url)
    if parsed.scheme not in {"smtp", "smtps", "smtp+ssl"}:
        raise DeliveryError("EMAIL_SMTP_URL must start with smtp:// or smtps://", code="E_SMTP_CONFIG")
    host = parsed.hostname or "localhost"
    use_ssl = parsed.scheme in {"smtps", "smtp+ssl"}
    port = parsed.port or (465 if use_ssl else 587)
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    cc = _parse_recipient_list(settings.email_cc)
    bcc = _parse_recipient_list(settings.email_bcc)
    subject = settings.email_subject or f"FundSignal Delivery — {scoring_run_id}"
    return SMTPDeliveryConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        use_ssl=use_ssl,
        disable_tls=bool(settings.email_disable_tls),
        from_address=settings.email_from,
        to_addresses=recipients,
        cc_addresses=cc,
        bcc_addresses=bcc,
        subject=subject,
    )


def _parse_recipient_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def main() -> None:
    """Entry point for `python -m pipelines.day3.email_delivery`."""
    try:
        run()
    except DeliveryError as exc:
        logger.error("delivery.email.failed", extra={"code": exc.code, "error": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
