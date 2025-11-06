"""Tavily cross-confirmation pipeline for press-verified leads."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from app.clients.tavily import (
    TavilyClient,
    TavilyError,
    TavilyNotFoundError,
    TavilyRateLimitError,
    TavilyTimeoutError,
)
from app.models.lead import CompanyFunding
from scripts.backoff import exponential_backoff

logger = logging.getLogger("pipelines.day1.tavily_confirm")

TavilyResponse = Mapping[str, Any]
SleepFn = Callable[[float], None]
_P95_TARGET = 0.95

@dataclass
class TavilyResult:
    """Normalized Tavily search result."""

    url: str
    title: str
    snippet: str


def build_query(lead: CompanyFunding) -> str:
    """Build Tavily query string."""
    amount_str = _format_query_amount(lead.funding_amount)
    return f"{lead.company} {lead.funding_stage} funding raised {amount_str}"


def normalize_results(raw_results: Sequence[TavilyResponse]) -> list[TavilyResult]:
    """Normalize raw Tavily JSON to TavilyResult."""
    normalized: list[TavilyResult] = []
    for result in raw_results:
        url = result.get("url") or result.get("link")
        title = (result.get("title") or "").strip()
        snippet = (result.get("snippet") or result.get("content") or "").strip()
        if not url or not title:
            continue
        normalized.append(TavilyResult(url=url, title=title, snippet=snippet))
    return normalized


def _canonical_domain(url: str) -> str:
    """Extract a canonical domain suitable for deduplication."""
    netloc = urlparse(url).netloc.lower()
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    parts = [part for part in netloc.split(".") if part]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return netloc


def filter_confirming_results(
    results: Iterable[TavilyResult],
    *,
    lead: CompanyFunding,
) -> list[TavilyResult]:
    """Filter Tavily results to those confirming funding details."""
    confirming: list[TavilyResult] = []
    seen_domains: set[str] = set()

    amount_tokens = format_amount_tokens(lead.funding_amount)
    company_lower = lead.company.lower()
    stage_lower = lead.funding_stage.lower()

    for result in results:
        haystack = f"{result.title} {result.snippet}".lower()
        if company_lower not in haystack:
            continue
        if not any(token in haystack for token in amount_tokens) and stage_lower not in haystack:
            continue

        domain = _canonical_domain(result.url)
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        confirming.append(result)
    return confirming


def format_amount_tokens(amount: int) -> set[str]:
    """Tokens used to match funding amount mentions."""
    if amount <= 0:
        return set()

    tokens: set[str] = {
        f"{amount:,}".lower(),
        f"${amount:,}".lower(),
    }

    def _add_tokens(value: float, suffix: str, word: str) -> None:
        normalized = f"{value:.1f}".rstrip("0").rstrip(".")
        variants = {
            f"{normalized}{suffix}",
            f"${normalized}{suffix}",
            f"{normalized} {word}",
            f"${normalized} {word}",
        }
        tokens.update(token.lower() for token in variants)

    if amount >= 1_000_000:
        _add_tokens(amount / 1_000_000, "m", "million")
    elif amount >= 1_000:
        _add_tokens(amount / 1_000, "k", "thousand")

    return tokens


def discover_with_retries(
    client: TavilyClient,
    *,
    query: str,
    max_results: int,
    max_attempts: int = 5,
    sleep: SleepFn | None = None,
) -> list[TavilyResult]:
    """Call Tavily with retries."""
    if max_results <= 0:
        raise ValueError("max_results must be a positive integer.")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    sleeper = sleep or time.sleep
    for attempt, delay in exponential_backoff(max_attempts=max_attempts, base_delay=0.5):
        try:
            raw = client.search(query=query, max_results=max_results)
            return normalize_results(raw)
        except (TavilyRateLimitError, TavilyTimeoutError) as exc:
            if attempt >= max_attempts:
                raise
            logger.warning(
                "Tavily transient error (%s). Attempt %s/%s; retrying in %.1fs.",
                exc.code,
                attempt,
                max_attempts,
                delay,
            )
            sleeper(delay)
    raise TavilyError("Unable to complete Tavily confirmation after retries.")


def run_confirmation(
    leads: list[CompanyFunding],
    *,
    client: TavilyClient,
    min_confirmations: int,
    max_results: int,
    sleep: SleepFn | None = None,
) -> None:
    """Mutate leads with Tavily verification."""
    if min_confirmations < 1:
        raise ValueError("min_confirmations must be >= 1")

    durations: list[float] = []
    sleeper = sleep or time.sleep

    for lead in leads:
        if not lead.youcom_verified:
            lead.tavily_reason = "pending_youcom_verification"
            continue

        query = build_query(lead)
        logger.debug("Confirming %s via Tavily. query=%s", lead.company, query)
        try:
            results = discover_with_retries(
                client,
                query=query,
                max_results=max_results,
                sleep=sleeper,
            )
        except TavilyNotFoundError:
            lead.tavily_reason = "insufficient_sources"
            logger.info("No Tavily confirmations for %s.", lead.company)
            continue
        except TavilyError as exc:
            lead.tavily_reason = exc.code
            logger.error("Tavily confirmation failed for %s: %s", lead.company, exc)
            continue

        start = time.perf_counter()
        confirming = filter_confirming_results(results, lead=lead)
        durations.append(time.perf_counter() - start)

        if len(confirming) < min_confirmations:
            lead.tavily_reason = "insufficient_sources"
            logger.info(
                "Insufficient Tavily confirmations for %s (found %s, need %s).",
                lead.company,
                len(confirming),
                min_confirmations,
            )
            continue

        lead.tavily_verified = True
        lead.tavily_verified_at = datetime.now(tz=timezone.utc)
        lead.tavily_reason = None
        lead.proof_links = [result.url for result in confirming[: min_confirmations]]
        logger.info(
            "Tavily verified %s with %s confirming sources. sample=%s",
            lead.company,
            len(confirming),
            lead.proof_links[:2],
        )

    p95 = _percentile_ms(durations, _P95_TARGET)
    total_verified = sum(1 for lead in leads if lead.tavily_verified)
    logger.info(
        "Tavily confirmation summary: total=%s verified=%s rate=%.1f%% processing_p95=%.1fms",
        len(leads),
        total_verified,
        (total_verified / len(leads) * 100) if leads else 0.0,
        p95,
    )


def load_leads(path: Path) -> list[CompanyFunding]:
    """Load CompanyFunding records from JSON."""
    with path.open("r", encoding="utf-8") as infile:
        data = json.load(infile)
    if not isinstance(data, list):
        raise ValueError("Input JSON must be a list.")
    return [CompanyFunding.model_validate(item) for item in data]


def persist_leads(leads: list[CompanyFunding], path: Path) -> None:
    """Persist leads to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as outfile:
        json.dump([lead.model_dump(mode="json") for lead in leads], outfile, indent=2)
        outfile.write("\n")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI args."""
    parser = argparse.ArgumentParser(description="Confirm funding via Tavily search.")
    parser.add_argument("--input", type=Path, default=Path("leads/youcom_verified.json"), help="Input JSON path.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("leads/tavily_confirmed.json"),
        help="Output JSON path.",
    )
    parser.add_argument("--min_confirmations", type=int, default=2, help="Minimum confirming sources required.")
    parser.add_argument("--max_results", type=int, default=8, help="Number of Tavily results to request.")
    return parser.parse_args(argv)


def run_pipeline(
    *,
    input_path: Path,
    output_path: Path,
    min_confirmations: int,
    max_results: int,
    client: TavilyClient | None = None,
) -> list[CompanyFunding]:
    """Run the Tavily confirmation pipeline."""
    leads = load_leads(input_path)
    logger.info("Loaded %s leads from %s.", len(leads), input_path)

    client_owned = False
    if client is None:
        try:
            client = TavilyClient.from_env()
        except ValueError as exc:
            raise TavilyError(str(exc)) from exc
        client_owned = True

    try:
        run_confirmation(
            leads,
            client=client,
            min_confirmations=min_confirmations,
            max_results=max_results,
        )
    finally:
        if client_owned and client is not None:
            client.close()

    persist_leads(leads, output_path)
    logger.info("Persisted %s leads to %s.", len(leads), output_path)
    return leads


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv or sys.argv[1:])
    try:
        run_pipeline(
            input_path=args.input,
            output_path=args.output,
            min_confirmations=args.min_confirmations,
            max_results=args.max_results,
        )
    except TavilyError as exc:
        logger.error("Tavily confirmation failed: %s (code=%s)", exc, getattr(exc, "code", "TAVILY_ERROR"))
        return 1
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unexpected error during Tavily confirmation: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())


def _format_query_amount(amount: int) -> str:
    """Format funding amount portion of Tavily queries."""
    if amount >= 1_000_000:
        normalized = f"{amount / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"${normalized}M"
    if amount >= 1_000:
        normalized = f"{amount / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"${normalized}K"
    return f"${amount:,}"


def _percentile_ms(values: Sequence[float], percentile: float) -> float:
    """Compute percentile of durations in milliseconds."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(percentile * (len(ordered) - 1))
    return ordered[index] * 1000
