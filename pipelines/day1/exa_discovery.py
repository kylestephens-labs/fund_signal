"""Exa discovery pipeline for recently funded B2B SaaS companies."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.clients.exa import (
    ExaClient,
    ExaError,
    ExaRateLimitError,
    ExaSchemaError,
    ExaTimeoutError,
)
from app.models.lead import CompanyFunding
from scripts.backoff import exponential_backoff

logger = logging.getLogger("pipelines.day1.exa_discovery")

ExaResult = Mapping[str, Any]
SleepFn = Callable[[float], None]

DEFAULT_QUERY_TEMPLATE = (
    "B2B SaaS companies that announced Seed, Series A, or Series B funding "
    "between {days_min} and {days_max} days ago. Include funding amount, stage, and date."
)
TRIGGER_WORDS = [" raises ", " lands ", " secures ", " closes ", " scores "]
STAGE_KEYWORDS = ["pre-seed", "seed", "angel", "growth"]
SERIES_PATTERN = re.compile(r"\bseries [a-z0-9]{1,2}\b", flags=re.IGNORECASE)
AMOUNT_PATTERN = re.compile(r"\$?([\d,.]+)\s?(billion|million|thousand|m|b|k)?", flags=re.IGNORECASE)
EXA_TEXT_FIELDS = ("title", "summary", "text", "raw_content")
MIN_REQUIRED_COMPANIES = 50


def _log_retry_event(
    *,
    provider: str,
    code: str,
    attempt: int,
    max_attempts: int,
    delay: float,
    query: str,
) -> None:
    logger.warning(
        "provider.retry",
        extra={
            "provider": provider,
            "code": code,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "delay_ms": round(delay * 1000, 2),
            "query": query[:120],
        },
    )


def build_query(days_min: int, days_max: int) -> str:
    """Compose a focused Exa query string."""
    return DEFAULT_QUERY_TEMPLATE.format(days_min=days_min, days_max=days_max)


def discover_with_retries(
    client: ExaClient,
    *,
    query: str,
    days_min: int,
    days_max: int,
    limit: int,
    max_attempts: int = 5,
    sleep: SleepFn | None = None,
) -> Sequence[dict]:
    """Invoke Exa with exponential backoff on retryable errors."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    sleeper = sleep or time.sleep
    for attempt, delay in exponential_backoff(max_attempts=max_attempts):
        try:
            return client.search_recent_funding(
                query=query,
                days_min=days_min,
                days_max=days_max,
                limit=limit,
            )
        except (ExaRateLimitError, ExaTimeoutError) as exc:
            if attempt >= max_attempts:
                raise
            _log_retry_event(
                provider="exa",
                code=exc.code,
                attempt=attempt,
                max_attempts=max_attempts,
                delay=delay,
                query=query,
            )
            sleeper(delay)
    raise ExaError("Unable to complete Exa discovery after retries.")


def parse_company(result: Mapping[str, Any]) -> str | None:
    """Extract company name from Exa result heuristics."""
    title = (result.get("title") or "").strip()
    if not title:
        return None

    lowered = title.lower()
    for trigger in TRIGGER_WORDS:
        if trigger in lowered:
            index = lowered.index(trigger)
            return title[:index].replace("—", "-").strip(" -")

    return title.split(":", 1)[0].strip() if title else None


def parse_stage(text: str) -> str | None:
    """Detect funding stage keywords."""
    text_lower = text.lower()
    series_match = SERIES_PATTERN.search(text_lower)
    if series_match:
        return series_match.group(0).title()
    for stage in STAGE_KEYWORDS:
        if stage in text_lower:
            return stage.title()
    return None


def parse_amount(text: str) -> int | None:
    """Parse funding amount from text, returning integer USD amount."""
    best_amount: int | None = None
    for match in AMOUNT_PATTERN.finditer(text):
        token = match.group(0)
        multiplier_label = match.group(2)
        # Skip plain integers without currency context to avoid catching IDs in names.
        if not token.startswith("$") and not multiplier_label:
            continue

        raw_value = match.group(1).replace(",", "")
        try:
            value = float(raw_value)
        except ValueError:
            continue

        multiplier = 1
        if multiplier_label:
            label = multiplier_label.lower()
            if label in {"billion", "b"}:
                multiplier = 1_000_000_000
            elif label in {"million", "m"}:
                multiplier = 1_000_000
            elif label in {"thousand", "k"}:
                multiplier = 1_000

        amount = int(value * multiplier)
        if amount <= 0:
            continue
        best_amount = max(best_amount or 0, amount)

    return best_amount


def parse_date_from_result(result: Mapping[str, Any]) -> datetime | None:
    """Extract announcement date from known Exa fields."""
    date_candidates = [
        result.get("publishedDate"),
        result.get("publishDate"),
        result.get("published_date"),
        result.get("date"),
    ]
    for candidate in date_candidates:
        if not candidate:
            continue
        parsed = _parse_datetime(candidate)
        if parsed:
            return parsed
    # fallback to timestamp from metadata if available
    metadata = result.get("metadata") or {}
    if isinstance(metadata, dict):
        for key in ("published_at", "timestamp"):
            candidate = metadata.get(key)
            if candidate:
                parsed = _parse_datetime(candidate)
                if parsed:
                    return parsed
    return None


def _parse_datetime(value: str) -> datetime | None:
    """Attempt to parse datetime from multiple formats."""
    formats = (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            continue
    # ISO parsing fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_results(results: Iterable[ExaResult]) -> list[CompanyFunding]:
    """Convert raw Exa results to CompanyFunding models."""
    normalized: list[CompanyFunding] = []
    discovered_at = datetime.now(tz=UTC)
    for result in results:
        record = normalize_result(result, discovered_at)
        if record is not None:
            normalized.append(record)
    return normalized


def normalize_result(result: ExaResult, discovered_at: datetime) -> CompanyFunding | None:
    """Normalize a single Exa result into a CompanyFunding record."""
    article_text = _compose_article_text(result)
    company = parse_company(result)
    amount = parse_amount(article_text)
    stage = parse_stage(article_text)
    date_value = parse_date_from_result(result)
    source_url = result.get("url") or result.get("source_url")

    if not all([company, amount, stage, date_value, source_url]):
        logger.debug(
            "Skipping result missing required fields. company=%s amount=%s stage=%s date=%s url=%s",
            company,
            amount,
            stage,
            date_value.isoformat() if date_value else None,
            source_url,
        )
        return None

    return CompanyFunding(
        company=company,
        funding_amount=amount,
        funding_stage=stage,
        funding_date=date_value.date(),
        source_url=source_url,
        discovered_at=discovered_at,
    )


def _compose_article_text(result: ExaResult) -> str:
    """Concatenate relevant text fields from an Exa result."""
    parts = [value for field in EXA_TEXT_FIELDS if (value := result.get(field))]
    return " ".join(parts)


def persist_records(records: Sequence[CompanyFunding], output_path: Path) -> None:
    """Persist normalized funding records to disk with idempotent upsert."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_existing_records(output_path)
    for record in records:
        data = record.model_dump(mode="json")
        existing[_record_key(data)] = data

    _write_records(output_path, existing.values())


def _record_key(data: dict) -> str:
    """Create a deterministic key for upsert operations."""
    company = data.get("company")
    funding_date = data.get("funding_date")
    if not company or not funding_date:
        raise ExaSchemaError("Company or funding_date missing from record during persistence.")
    return f"{company.lower()}::{funding_date}"


def _load_existing_records(output_path: Path) -> dict[str, dict[str, Any]]:
    """Load previously persisted records for upsert operations."""
    if not output_path.exists():
        return {}
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Existing file %s is not valid JSON. Overwriting.", output_path)
        return {}
    except OSError as exc:  # pragma: no cover - filesystem edge cases
        logger.warning("Unable to read existing records from %s: %s", output_path, exc)
        return {}

    if not isinstance(payload, list):
        logger.warning("Existing file %s is not a list. Overwriting.", output_path)
        return {}

    existing: dict[str, dict[str, Any]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        try:
            key = _record_key(entry)
        except ExaSchemaError as exc:
            logger.debug("Skipping entry missing required fields: %s", exc)
            continue
        existing[key] = entry
    return existing


def _write_records(output_path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Write normalized records to disk with deterministic formatting."""
    serialized = list(records)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump(serialized, outfile, indent=2)
        outfile.write("\n")


def run_pipeline(
    *,
    days_min: int,
    days_max: int,
    limit: int,
    output_file: Path,
    client: ExaClient | None = None,
) -> list[CompanyFunding]:
    """Run the Exa discovery pipeline end-to-end."""
    query = build_query(days_min, days_max)
    logger.info(
        "Running Exa discovery. days_min=%s days_max=%s limit=%s output=%s",
        days_min,
        days_max,
        limit,
        output_file,
    )

    client_owned = False
    if client is None:
        try:
            client = ExaClient.from_env()
        except ValueError as exc:
            raise ExaError(str(exc)) from exc
        client_owned = True

    try:
        raw_results = discover_with_retries(
            client,
            query=query,
            days_min=days_min,
            days_max=days_max,
            limit=limit,
        )
        logger.info("Exa returned %s raw results.", len(raw_results))

        normalized = normalize_results(raw_results)
        logger.info("Parsed %s candidate companies.", len(normalized))

        if len(normalized) < MIN_REQUIRED_COMPANIES:
            raise ExaError(
                f"Parsed {len(normalized)} companies, which is below the target of {MIN_REQUIRED_COMPANIES}. "
                "Adjust query or inspect parsing heuristics."
            )

        persist_records(normalized, output_file)
        logger.info("Persisted %s records to %s.", len(normalized), output_file)
        return normalized
    finally:
        if client_owned and client is not None:
            client.close()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """CLI argument parsing."""
    parser = argparse.ArgumentParser(description="Seed Exa discovery for recently funded B2B SaaS.")
    parser.add_argument("--days_min", type=int, default=60, help="Minimum number of days since announcement.")
    parser.add_argument("--days_max", type=int, default=90, help="Maximum number of days since announcement.")
    parser.add_argument("--limit", type=int, default=80, help="Number of Exa results to request.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("leads/exa_seed.json"),
        help="Path to output JSON file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for Exa discovery pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = parse_args(argv or sys.argv[1:])
    try:
        run_pipeline(
            days_min=args.days_min,
            days_max=args.days_max,
            limit=args.limit,
            output_file=args.output,
        )
    except ExaError as exc:
        logger.error("Exa discovery failed: %s (code=%s)", exc, getattr(exc, "code", "EXA_ERROR"))
        return 1
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unexpected error during Exa discovery: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
