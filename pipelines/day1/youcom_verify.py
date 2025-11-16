"""You.com verification pipeline for Exa funding candidates."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.clients.youcom import (
    YoucomError,
    YoucomNotFoundError,
    YoucomRateLimitError,
    YoucomTimeoutError,
)
from app.models.lead import CompanyFunding
from pipelines.io.fixture_loader import FixtureArtifactSpec, resolve_bundle_context
from pipelines.news_client import YoucomClientProtocol, get_runtime_config, get_youcom_client
from scripts.backoff import exponential_backoff

logger = logging.getLogger("pipelines.day1.youcom_verify")

YoucomResult = Mapping[str, Any]
SleepFn = Callable[[float], None]
_P95_TARGET = 0.95


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

@dataclass
class ArticleEvidence:
    """Normalized view of a You.com article result."""

    url: str
    publisher: str
    title: str
    snippet: str


def format_amount_tokens(amount: int) -> set[str]:
    """Create string tokens representing the funding amount."""
    if amount <= 0:
        return set()

    tokens: set[str] = {
        f"{amount:,}",
        f"${amount:,}",
    }

    def _add_human_tokens(value: float, suffix: str, word_suffix: str) -> None:
        normalized = f"{value:.1f}".rstrip("0").rstrip(".")
        tokens.update(
            {
                f"{normalized}{suffix}",
                f"${normalized}{suffix}",
                f"{normalized} {word_suffix}",
                f"${normalized} {word_suffix}",
            }
        )

    if amount >= 1_000_000:
        millions = amount / 1_000_000
        _add_human_tokens(millions, "m", "million")
    elif amount >= 1_000:
        thousands = amount / 1_000
        _add_human_tokens(thousands, "k", "thousand")

    return {token.lower() for token in tokens}


def _canonical_domain(url: str) -> str:
    """Extract a canonical domain for deduplication."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def dedupe_articles(articles: Iterable[ArticleEvidence]) -> list[ArticleEvidence]:
    """Remove duplicate articles by canonical domain."""
    seen_domains: set[str] = set()
    unique: list[ArticleEvidence] = []
    for article in articles:
        domain = _canonical_domain(article.url)
        if not domain:
            continue
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        unique.append(article)
    return unique


def article_confirms_funding(
    article: ArticleEvidence,
    *,
    company: str,
    funding_stage: str,
    funding_amount: int,
) -> bool:
    """Determine whether an article confirms the funding details."""
    haystack = f"{article.title} {article.snippet}".lower()
    company_normalized = company.lower()
    if company_normalized not in haystack:
        return False

    stage_token = funding_stage.lower()
    amount_tokens = format_amount_tokens(funding_amount)
    amount_match = any(token in haystack for token in amount_tokens)

    return stage_token in haystack or amount_match


def normalize_youcom_results(raw_results: Sequence[YoucomResult]) -> list[ArticleEvidence]:
    """Convert raw You.com results into ArticleEvidence items."""
    normalized: list[ArticleEvidence] = []
    for result in raw_results:
        url = result.get("url")
        title = (result.get("title") or "").strip()
        snippet = (result.get("snippet") or result.get("summary") or "").strip()
        publisher = (result.get("publisher") or result.get("source") or "").strip()
        if not url or not title or not publisher:
            continue
        normalized.append(
            ArticleEvidence(
                url=url,
                publisher=publisher,
                title=title,
                snippet=snippet,
            )
        )
    return normalized


def discover_with_retries(
    client: YoucomClientProtocol,
    *,
    query: str,
    limit: int,
    max_attempts: int = 5,
    sleep: SleepFn | None = None,
) -> list[ArticleEvidence]:
    """Query You.com with retries on transient errors."""
    if limit <= 0:
        raise ValueError("limit must be a positive integer.")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    sleeper = sleep or time.sleep
    for attempt, delay in exponential_backoff(max_attempts=max_attempts, base_delay=0.5):
        try:
            raw_results = client.search_news(query=query, limit=limit)
            return normalize_youcom_results(raw_results)
        except (YoucomRateLimitError, YoucomTimeoutError) as exc:
            if attempt >= max_attempts:
                raise
            _log_retry_event(
                provider="youcom",
                code=exc.code,
                attempt=attempt,
                max_attempts=max_attempts,
                delay=delay,
                query=query,
            )
            sleeper(delay)
    raise YoucomError("Unable to complete You.com verification after retries.")


def build_query(lead: CompanyFunding) -> str:
    """Construct a You.com query for the company."""
    amount_str = _format_query_amount(lead.funding_amount)
    return f"{lead.company} {lead.funding_stage} funding {amount_str}"


def _format_query_amount(amount: int) -> str:
    """Format funding amount for use within You.com query strings."""
    if amount >= 1_000_000:
        millions = amount / 1_000_000
        normalized = f"{millions:.1f}".rstrip("0").rstrip(".")
        return f"${normalized}M"
    if amount >= 1_000:
        thousands = amount / 1_000
        normalized = f"{thousands:.1f}".rstrip("0").rstrip(".")
        return f"${normalized}K"
    return f"${amount:,}"


def select_confirming_articles(
    articles: Iterable[ArticleEvidence],
    lead: CompanyFunding,
) -> list[ArticleEvidence]:
    """Return deduplicated articles that confirm the lead's funding details."""
    deduped = dedupe_articles(articles)
    return [
        article
        for article in deduped
        if article_confirms_funding(
            article,
            company=lead.company,
            funding_stage=lead.funding_stage,
            funding_amount=lead.funding_amount,
        )
    ]


def verify_leads(
    leads: list[CompanyFunding],
    *,
    client: YoucomClientProtocol,
    min_articles: int,
    max_results: int,
    sleep: SleepFn | None = None,
) -> None:
    """Mutate leads in-place with You.com verification metadata."""
    if min_articles < 1:
        raise ValueError("min_articles must be >= 1")

    verified_count = 0
    durations: list[float] = []
    sleeper = sleep or time.sleep

    for lead in leads:
        query = build_query(lead)
        logger.debug("Verifying %s via You.com. query=%s", lead.company, query)
        try:
            articles = discover_with_retries(
                client,
                query=query,
                limit=max_results,
                sleep=sleeper,
            )
        except YoucomNotFoundError:
            logger.info("No press coverage found for %s.", lead.company)
            continue
        except YoucomError as exc:
            logger.error("You.com verification failed for %s: %s", lead.company, exc)
            continue

        start_process = time.perf_counter()
        confirming = select_confirming_articles(articles, lead)
        durations.append(time.perf_counter() - start_process)

        if len(confirming) < min_articles:
            logger.info(
                "Insufficient confirming articles for %s (found %s, need %s).",
                lead.company,
                len(confirming),
                min_articles,
            )
            continue

        timestamp = datetime.now(tz=UTC)
        lead.youcom_verified = True
        lead.youcom_verified_at = timestamp
        lead.news_sources = list(dict.fromkeys(article.publisher for article in confirming))
        lead.press_articles = [article.url for article in confirming]
        verified_count += 1

    p95_ms = _percentile_ms(durations, _P95_TARGET)
    logger.info(
        "You.com verification complete. total=%s verified=%s rate=%.1f%% processing_p95=%.1fms",
        len(leads),
        verified_count,
        (verified_count / len(leads) * 100) if leads else 0.0,
        p95_ms,
    )


def _percentile_ms(values: Sequence[float], percentile: float) -> float:
    """Calculate percentile latency in milliseconds."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = int(percentile * (len(sorted_values) - 1))
    return sorted_values[index] * 1000


def load_leads(input_path: Path) -> list[CompanyFunding]:
    """Load CompanyFunding records from JSON."""
    with input_path.open("r", encoding="utf-8") as infile:
        payload = json.load(infile)
    if not isinstance(payload, list):
        raise ValueError("Input JSON must be a list of funding records.")
    leads = [CompanyFunding.model_validate(item) for item in payload]
    return leads


def persist_leads(leads: list[CompanyFunding], output_path: Path) -> None:
    """Persist verified leads to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump([lead.model_dump(mode="json") for lead in leads], outfile, indent=2)
        outfile.write("\n")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Verify Exa candidates via You.com News.")
    parser.add_argument("--input", type=Path, default=Path("leads/exa_seed.json"), help="Path to Exa seed JSON.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("leads/youcom_verified.json"),
        help="Path to write verified leads JSON.",
    )
    parser.add_argument("--min_articles", type=int, default=2, help="Minimum distinct articles required.")
    parser.add_argument("--max_results", type=int, default=8, help="Maximum articles to request from You.com.")
    return parser.parse_args(argv)


DEFAULT_INPUT = Path("leads/exa_seed.json")
DEFAULT_OUTPUT = Path("leads/youcom_verified.json")

YOUCOM_INPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_INPUT, location="raw_dir")
YOUCOM_OUTPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_OUTPUT, location="leads_dir")


def run_pipeline(
    *,
    input_path: Path,
    output_path: Path,
    min_articles: int,
    max_results: int,
    client: YoucomClientProtocol | None = None,
) -> list[CompanyFunding]:
    """Run the You.com verification pipeline end-to-end."""
    config = get_runtime_config()
    context = resolve_bundle_context(
        config,
        input_path=input_path,
        output_path=output_path,
        input_spec=YOUCOM_INPUT_SPEC,
        output_spec=YOUCOM_OUTPUT_SPEC,
    )
    leads = load_leads(context.input_path)
    logger.info("Loaded %s Exa candidates from %s.", len(leads), context.input_path)

    close_fn = None
    if client is None:
        client = get_youcom_client(config)
        close_fn = getattr(client, "close", None)

    try:
        verify_leads(
            leads,
            client=client,
            min_articles=min_articles,
            max_results=max_results,
        )
    finally:
        if close_fn:
            close_fn()

    persist_leads(leads, context.output_path)
    logger.info("Persisted %s leads to %s.", len(leads), context.output_path)
    return leads


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for You.com verification pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = parse_args(argv or sys.argv[1:])
    try:
        run_pipeline(
            input_path=args.input,
            output_path=args.output,
            min_articles=args.min_articles,
            max_results=args.max_results,
        )
    except YoucomError as exc:
        logger.error("You.com verification failed: %s (code=%s)", exc, getattr(exc, "code", "YOUCOM_ERROR"))
        return 1
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unexpected error during You.com verification: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
