"""Render Day-3 email digests from persisted CompanyScore rows and optionally deliver via SMTP."""

from __future__ import annotations

import argparse
import csv
import html
import logging
import smtplib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse

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
HTML_OUTPUT_SUFFIX = ".html"
CSV_OUTPUT_SUFFIX = ".csv"
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
        help=f"Destination for the rendered Markdown; HTML/CSV siblings share the same stem (default: {DEFAULT_OUTPUT})",
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


def render_email(
    scoring_run_id: str, scores: Sequence[CompanyScore], *, generated_at: str | None = None
) -> str:
    """Return a Markdown digest for the supplied scoring run."""
    timestamp = generated_at or utc_now()
    feedback_href = _build_feedback_link(
        scoring_run_id=scoring_run_id,
        generated_at=timestamp,
        score_count=len(scores),
    )
    lines: list[str] = [
        f"# FundSignal Delivery — Run {scoring_run_id}",
        "",
        f"_Generated at {timestamp}_",
        "",
    ]
    if feedback_href:
        lines.extend(
            [
                f"[Provide feedback]({feedback_href}) (opens your email client)",
                "",
            ]
        )
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
                    verifiers = (
                        ", ".join(proof["verified_by"] or [])
                        if proof["verified_by"]
                        else "FundSignal"
                    )
                    lines.append(f"  - [{url}]({url}) _(verified by {verifiers})_")
            else:
                lines.append("  - _(No proofs available; flagged for follow-up)_")
        lines.append("")
    if not scores:
        lines.append("No companies qualified for this scoring run.")
    return "\n".join(lines).strip() + "\n"


def render_email_html(
    scoring_run_id: str,
    scores: Sequence[CompanyScore],
    *,
    csv_href: str,
    generated_at: str,
) -> str:
    """Return an HTML digest mirroring the Slack payload with a CSV download link."""
    feedback_href = _build_feedback_link(
        scoring_run_id=scoring_run_id,
        generated_at=generated_at,
        score_count=len(scores),
    )
    parts: list[str] = [
        "<html>",
        "<body>",
        f"<h1>FundSignal Delivery — Run {html.escape(scoring_run_id)}</h1>",
        f"<p><em>Generated at {html.escape(generated_at)}</em></p>",
        f'<p><strong><a href="{html.escape(csv_href)}">Download CSV</a></strong> (attached)</p>',
        "<ol>",
    ]
    if feedback_href:
        parts.append(
            f'<p><a href="{html.escape(feedback_href)}">Provide feedback</a> '
            "(opens your email client)</p>"
        )
    for index, score in enumerate(scores, start=1):
        confidence = compute_confidence(score.score)
        parts.append("<li>")
        parts.append(
            f"<h2>{index}. {html.escape(str(score.company_id))} — {score.score} pts ({confidence})</h2>"
        )
        parts.append("<ul>")
        parts.append(
            f"<li><strong>Recommended approach:</strong> {html.escape(score.recommended_approach)}</li>"
        )
        parts.append(f"<li><strong>Pitch angle:</strong> {html.escape(score.pitch_angle)}</li>")
        proof_items = _render_proof_links(score)
        if proof_items:
            parts.append("<li><strong>Proofs:</strong><ul>")
            parts.extend(proof_items)
            parts.append("</ul></li>")
        else:
            parts.append("<li><strong>Proofs:</strong> <em>None provided</em></li>")
        parts.append("</ul>")
        parts.append("</li>")
    parts.append("</ol>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def _render_proof_links(score: CompanyScore, max_items: int = 2) -> list[str]:
    items: list[str] = []
    for entry in score.breakdown:
        for proof in summarize_proofs(entry):
            if len(items) >= max_items:
                break
            url = html.escape(proof["source_url"])
            label = html.escape(entry.reason)
            verified_by = ", ".join(proof["verified_by"] or []) if proof["verified_by"] else None
            suffix = f" ({html.escape(verified_by)})" if verified_by else ""
            items.append(f'<li><a href="{url}">{label}</a>{suffix}</li>')
        if len(items) >= max_items:
            break
    return items


def _build_feedback_link(*, scoring_run_id: str, generated_at: str, score_count: int) -> str | None:
    """Construct a mailto link with basic run context."""
    recipient = settings.email_feedback_to
    if not recipient:
        return None
    subject = f"FundSignal feedback — Run {scoring_run_id}"
    body = (
        "I have feedback on this FundSignal delivery.\n\n"
        f"- Run ID: {scoring_run_id}\n"
        f"- Generated at: {generated_at}\n"
        f"- Companies in this email: {score_count}\n\n"
        "Feedback:\n"
        "- What looks wrong/right:\n"
        "- Specific leads (if any):\n"
        "- Links/screenshots (optional):\n"
    )
    return f"mailto:{recipient}?subject={quote_plus(subject)}&body={quote_plus(body)}"


def _write_csv(
    destination: Path,
    scoring_run_id: str,
    scores: Sequence[CompanyScore],
    *,
    generated_at: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "company_id",
        "score",
        "confidence",
        "recommended_approach",
        "pitch_angle",
        "proofs",
        "scoring_run_id",
        "generated_at",
    ]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for score in scores:
            proof_urls = [
                proof["source_url"] or ""
                for entry in score.breakdown
                for proof in summarize_proofs(entry)
                if proof.get("source_url")
            ]
            writer.writerow(
                {
                    "company_id": score.company_id,
                    "score": score.score,
                    "confidence": compute_confidence(score.score),
                    "recommended_approach": score.recommended_approach,
                    "pitch_angle": score.pitch_angle,
                    "proofs": ", ".join(proof_urls),
                    "scoring_run_id": scoring_run_id,
                    "generated_at": generated_at,
                }
            )
    logger.info(
        "delivery.email.csv_written",
        extra={"scoring_run_id": scoring_run_id, "count": len(scores), "output": str(destination)},
    )
    metrics.increment(
        "delivery.email.csv_written",
        tags={"scoring_run": scoring_run_id, "count": len(scores)},
    )


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
    generated_at = utc_now()
    if args.force_refresh:
        logger.info(
            "delivery.email.force_refresh",
            extra={"scoring_run_id": scoring_run_id},
        )
    payload = render_email(scoring_run_id, filtered, generated_at=generated_at)
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload, encoding="utf-8")
    html_output = output_path.with_suffix(HTML_OUTPUT_SUFFIX)
    artifact_html = render_email_html(
        scoring_run_id,
        filtered,
        csv_href=html.escape(output_path.with_suffix(CSV_OUTPUT_SUFFIX).name),
        generated_at=generated_at,
    )
    html_output.write_text(artifact_html, encoding="utf-8")
    csv_output = output_path.with_suffix(CSV_OUTPUT_SUFFIX)
    _write_csv(csv_output, scoring_run_id, filtered, generated_at=generated_at)
    record_delivery_event(
        "email",
        scoring_run_id=scoring_run_id,
        count=len(filtered),
        output_path=str(output_path),
    )
    logger.info(
        "delivery.email.rendered",
        extra={
            "scoring_run_id": scoring_run_id,
            "output": str(output_path),
            "html_output": str(html_output),
            "csv_output": str(csv_output),
        },
    )
    if args.deliver:
        csv_content_id = make_msgid(domain="fundsignal.csv").strip("<>")
        email_html = render_email_html(
            scoring_run_id,
            filtered,
            csv_href=f"cid:{csv_content_id}",
            generated_at=generated_at,
        )
        _deliver_via_smtp(
            scoring_run_id, payload, email_html, output_path, csv_output, csv_content_id
        )
    return output_path


def _deliver_via_smtp(
    scoring_run_id: str,
    text_body: str,
    html_body: str,
    artifact_path: Path,
    csv_path: Path,
    csv_content_id: str,
) -> None:
    config = _build_smtp_config(scoring_run_id)
    message, recipients = _render_email_message(
        config, text_body, html_body, scoring_run_id, csv_path, csv_content_id
    )
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
    config: SMTPDeliveryConfig,
    text_body: str,
    html_body: str,
    scoring_run_id: str,
    csv_path: Path,
    csv_content_id: str,
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
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    if csv_path.exists():
        message.add_attachment(
            csv_path.read_bytes(),
            maintype="text",
            subtype="csv",
            filename=csv_path.name,
            cid=csv_content_id,
        )
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
        raise DeliveryError(
            "EMAIL_SMTP_URL must start with smtp:// or smtps://", code="E_SMTP_CONFIG"
        )
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
