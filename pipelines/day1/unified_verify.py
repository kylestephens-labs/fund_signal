"""Unified verification pipeline that merges You.com and Tavily signal attribution."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.clients.tavily import TavilyError
from app.clients.youcom import YoucomError
from pipelines.day1.article_normalizer import ArticleEvidence, ArticleNormalizer, slugify
from pipelines.io.fixture_loader import FixtureArtifactSpec, resolve_bundle_context
from pipelines.io.schemas import NormalizedSeed
from pipelines.news_client import RuntimeMode, get_runtime_config, get_tavily_client, get_youcom_client

logger = logging.getLogger("pipelines.day1.unified_verify")

DEFAULT_SEED = Path("leads/exa_seed.normalized.json")
DEFAULT_YOUCOM = Path("raw/youcom.jsonl.gz")
DEFAULT_TAVILY = Path("raw/tavily.jsonl.gz")
DEFAULT_OUTPUT = Path("leads/unified_verify.json")
UNIFIED_VERSION = "1.0.0"

SEED_SPEC = FixtureArtifactSpec(default_path=DEFAULT_SEED, location="leads_dir")
YOUCOM_SPEC = FixtureArtifactSpec(default_path=DEFAULT_YOUCOM, location="raw_dir")
TAVILY_SPEC = FixtureArtifactSpec(default_path=DEFAULT_TAVILY, location="raw_dir")
OUTPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_OUTPUT, location="leads_dir")

@dataclass(frozen=True)
class VerificationSource:
    """Descriptor for each verification provider."""

    id: str
    label: str


VERIFICATION_SOURCES: tuple[VerificationSource, ...] = (
    VerificationSource(id="youcom", label="You.com"),
    VerificationSource(id="tavily", label="Tavily"),
)


class UnifiedVerifyError(RuntimeError):
    """Raised when the unified verification pipeline cannot proceed."""

    def __init__(self, message: str, code: str = "UNIFIED_VERIFY_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LeadCandidate:
    """Normalized seed plus derived metadata."""

    lead_id: str
    slug: str
    seed: NormalizedSeed

    @property
    def company_name(self) -> str:
        return self.seed.company_name

    def normalized_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self.seed.funding_stage,
            "amount": self.seed.amount.model_dump(mode="json"),
            "source_url": str(self.seed.source_url),
        }
        if self.seed.announced_date:
            payload["announced_date"] = self.seed.announced_date.isoformat()
        if self.seed.raw_title:
            payload["raw_title"] = self.seed.raw_title
        if self.seed.raw_snippet:
            payload["raw_snippet"] = self.seed.raw_snippet
        return payload


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge You.com and Tavily verification into a unified artifact.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="Path to normalized Exa seed JSON.")
    parser.add_argument(
        "--youcom",
        type=Path,
        default=DEFAULT_YOUCOM,
        help="Fixture path for You.com jsonl.gz payloads (fixture mode only).",
    )
    parser.add_argument(
        "--tavily",
        type=Path,
        default=DEFAULT_TAVILY,
        help="Fixture path for Tavily jsonl.gz payloads (fixture mode only).",
    )
    parser.add_argument(
        "--output",
        "--out",
        dest="output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination for unified_verify.json",
    )
    parser.add_argument("--youcom-limit", type=int, default=8, help="Maximum articles to request from You.com.")
    parser.add_argument("--tavily-limit", type=int, default=8, help="Maximum articles to request from Tavily.")
    return parser.parse_args(argv)


def run_pipeline(
    *,
    seed_path: Path,
    youcom_path: Path,
    tavily_path: Path,
    output_path: Path,
    youcom_limit: int,
    tavily_limit: int,
) -> dict[str, Any]:
    """Execute the unified verification pipeline."""

    start = datetime.now(timezone.utc)
    config = get_runtime_config()
    context = resolve_bundle_context(
        config,
        input_path=seed_path,
        output_path=output_path,
        input_spec=SEED_SPEC,
        output_spec=OUTPUT_SPEC,
    )

    leads = _load_normalized_leads(context.input_path)
    youcom_input = _resolve_additional_path(config, youcom_path, YOUCOM_SPEC)
    tavily_input = _resolve_additional_path(config, tavily_path, TAVILY_SPEC)

    youcom_client = None
    tavily_client = None
    fixtures_youcom: dict[str, list[dict[str, Any]]] | None = None
    fixtures_tavily: dict[str, list[dict[str, Any]]] | None = None

    if config.mode is RuntimeMode.FIXTURE:
        fixtures_youcom = _load_fixture_index(youcom_input)
        fixtures_tavily = _load_fixture_index(tavily_input)
    else:
        youcom_client = get_youcom_client(config)
        tavily_client = get_tavily_client(config)

    try:
        payload = _verify_leads(
            leads,
            output_path=context.output_path,
            bundle_id=context.bundle.bundle_id if context.bundle else None,
            fixtures_youcom=fixtures_youcom,
            fixtures_tavily=fixtures_tavily,
            youcom_client=youcom_client,
            tavily_client=tavily_client,
            youcom_limit=youcom_limit,
            tavily_limit=tavily_limit,
            start=start,
        )
    finally:
        for closer in (getattr(youcom_client, "close", None), getattr(tavily_client, "close", None)):
            if closer:
                closer()
    return payload


def _verify_leads(
    leads: Sequence[LeadCandidate],
    *,
    output_path: Path,
    bundle_id: str | None,
    fixtures_youcom: dict[str, list[dict[str, Any]]] | None,
    fixtures_tavily: dict[str, list[dict[str, Any]]] | None,
    youcom_client,
    tavily_client,
    youcom_limit: int,
    tavily_limit: int,
    start: datetime,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_confirming_by_source = {source.id: 0 for source in VERIFICATION_SOURCES}
    unique_domains_global: set[str] = set()
    unique_domains_global_by_source: dict[str, set[str]] = {source.id: set() for source in VERIFICATION_SOURCES}

    for lead in leads:
        normalizer = ArticleNormalizer(lead.seed)
        by_source = {
            "youcom": _collect_articles(
                lead,
                source=_get_source("youcom"),
                normalizer=normalizer,
                fixtures=fixtures_youcom,
                client=youcom_client,
                limit=youcom_limit,
                query_builder=_build_youcom_query,
                api_fetch=_fetch_youcom_articles,
                normalize_records=_normalize_youcom_records,
                error_type=YoucomError,
            ),
            "tavily": _collect_articles(
                lead,
                source=_get_source("tavily"),
                normalizer=normalizer,
                fixtures=fixtures_tavily,
                client=tavily_client,
                limit=tavily_limit,
                query_builder=_build_tavily_query,
                api_fetch=_fetch_tavily_articles,
                normalize_records=_normalize_tavily_records,
                error_type=TavilyError,
            ),
        }

        lead_payload = _build_lead_payload(lead, by_source)
        results.append(lead_payload)

        _update_bundle_metrics(
            articles_by_source=by_source,
            total_confirming_by_source=total_confirming_by_source,
            unique_domains_global=unique_domains_global,
            unique_domains_global_by_source=unique_domains_global_by_source,
        )

    metrics = {
        "youcom_hits": total_confirming_by_source["youcom"],
        "tavily_hits": total_confirming_by_source["tavily"],
        "unique_domains_total": len(unique_domains_global),
        "unique_domains_by_source": {
            source_id: len(domains) for source_id, domains in unique_domains_global_by_source.items()
        },
    }

    payload = {
        "unified_verify_version": UNIFIED_VERSION,
        "generated_at": _format_timestamp(start),
        "bundle_id": bundle_id,
        "metrics": metrics,
        "leads": results,
    }
    _write_payload(payload, output_path)
    duration = datetime.now(timezone.utc) - start
    logger.info(
        "Unified verify complete. leads=%s youcom_hits=%s tavily_hits=%s unique_domains=%s duration=%.2fs",
        len(leads),
        metrics["youcom_hits"],
        metrics["tavily_hits"],
        metrics["unique_domains_total"],
        duration.total_seconds(),
    )
    return payload


def _load_normalized_leads(path: Path) -> list[LeadCandidate]:
    if not path.exists():
        raise UnifiedVerifyError(f"Normalized seed not found: {path}", code="SEED_NOT_FOUND")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UnifiedVerifyError(f"Invalid normalized seed JSON: {exc}", code="SEED_INVALID") from exc

    data = payload.get("data")
    if not isinstance(data, list):
        raise UnifiedVerifyError("Normalized seed payload missing data[]", code="SEED_INVALID")

    leads: list[LeadCandidate] = []
    slug_counts: dict[str, int] = {}
    for idx, entry in enumerate(data, start=1):
        if not isinstance(entry, Mapping):
            raise UnifiedVerifyError(f"Normalized seed entry {idx} must be an object.", code="SEED_INVALID")
        try:
            seed = NormalizedSeed.model_validate(entry)
        except Exception as exc:  # pragma: no cover - validation detail surfaces in tests
            raise UnifiedVerifyError(f"Invalid normalized seed at index {idx}: {exc}", code="SEED_INVALID") from exc
        slug = slugify(seed.company_name) or f"lead-{idx:04d}"
        slug_counts[slug] = slug_counts.get(slug, 0) + 1
        lead_id = slug if slug_counts[slug] == 1 else f"{slug}-{slug_counts[slug]}"
        leads.append(LeadCandidate(lead_id=lead_id, slug=slug, seed=seed))
    return leads


def _normalize_youcom_records(
    records: Iterable[Mapping[str, Any]],
    normalizer: ArticleNormalizer,
) -> list[ArticleEvidence]:
    normalized: list[ArticleEvidence] = []
    for record in records:
        url = record.get("url")
        title = record.get("title") or record.get("name")
        snippet = record.get("snippet") or record.get("summary") or record.get("description")
        published_at = record.get("published_at") or record.get("page_age") or record.get("date")
        evidence = normalizer.normalize(
            source_id="youcom",
            title=str(title) if title else None,
            snippet=str(snippet) if snippet else None,
            url=str(url) if url else None,
            published_at=str(published_at) if published_at else None,
        )
        if evidence:
            normalized.append(evidence)
    return normalized


def _normalize_tavily_records(
    records: Iterable[Mapping[str, Any]],
    normalizer: ArticleNormalizer,
) -> list[ArticleEvidence]:
    normalized: list[ArticleEvidence] = []
    for record in records:
        url = record.get("url") or record.get("link")
        title = record.get("title")
        snippet = record.get("content") or record.get("snippet") or record.get("summary")
        published_at = record.get("published_at") or record.get("published_date")
        evidence = normalizer.normalize(
            source_id="tavily",
            title=str(title) if title else None,
            snippet=str(snippet) if snippet else None,
            url=str(url) if url else None,
            published_at=str(published_at) if published_at else None,
        )
        if evidence:
            normalized.append(evidence)
    return normalized


def _build_youcom_query(seed: NormalizedSeed) -> str:
    amount = _format_query_amount(seed.amount)
    return f"{seed.company_name} {seed.funding_stage} funding {amount}"


def _build_tavily_query(seed: NormalizedSeed) -> str:
    amount = _format_query_amount(seed.amount)
    return f"{seed.company_name} {seed.funding_stage} funding raised {amount}"


def _format_query_amount(amount) -> str:
    unit = amount.unit.upper()
    value = amount.value
    suffix = {"K": "K", "M": "M", "B": "B"}.get(unit, "")
    return f"${value:g}{suffix}"


def _build_lead_payload(lead: LeadCandidate, articles_by_source: Mapping[str, Sequence[ArticleEvidence]]) -> dict[str, Any]:
    confirmations = {
        source.id: [article.to_confirmation() for article in articles_by_source.get(source.id, [])]
        for source in VERIFICATION_SOURCES
    }

    articles_all = _dedup_articles(articles_by_source)
    unique_domains_total = len({entry["domain"] for entry in articles_all})
    unique_domains_by_source = {
        source.id: len({article.domain for article in articles_by_source.get(source.id, []) if article.confirms})
        for source in VERIFICATION_SOURCES
    }

    verified_by = ["Exa"]
    for source in VERIFICATION_SOURCES:
        if any(article.confirms for article in articles_by_source.get(source.id, [])):
            verified_by.append(source.label)

    return {
        "id": lead.lead_id,
        "company_name": lead.company_name,
        "normalized": lead.normalized_payload(),
        "confirmations": confirmations,
        "articles_all": articles_all,
        "unique_domains_total": unique_domains_total,
        "unique_domains_by_source": unique_domains_by_source,
        "verified_by": verified_by,
    }


def _write_payload(payload: Mapping[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2)
        outfile.write("\n")


def _load_fixture_index(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        logger.warning("Fixture input missing: %s", path)
        return {}

    opener = gzip.open if path.suffix == ".gz" else open
    index: dict[str, list[dict[str, Any]]] = {}
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            if path.suffix == ".json":
                payload = json.load(handle)
                if isinstance(payload, dict) and isinstance(payload.get("data"), list) and payload.get("slug"):
                    slug = str(payload.get("slug"))
                    index[slug] = [item for item in payload["data"] if isinstance(item, dict)]
                    return index
            handle.seek(0)
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                slug = str(record.get("slug") or "")
                data = record.get("data")
                if not slug or not isinstance(data, list):
                    continue
                index[slug] = [item for item in data if isinstance(item, dict)]
    except OSError as exc:
        logger.warning("Unable to read fixture %s: %s", path, exc)
        return {}
    return index


def _resolve_additional_path(config, path: Path, spec: FixtureArtifactSpec) -> Path:
    context = resolve_bundle_context(
        config,
        input_path=path,
        output_path=path,
        input_spec=spec,
        output_spec=None,
        log=False,
    )
    return context.input_path


def _get_source(source_id: str) -> VerificationSource:
    for source in VERIFICATION_SOURCES:
        if source.id == source_id:
            return source
    raise UnifiedVerifyError(f"Unknown verification source: {source_id}")


def _collect_articles(
    lead: LeadCandidate,
    *,
    source: VerificationSource,
    normalizer: ArticleNormalizer,
    fixtures: dict[str, list[dict[str, Any]]] | None,
    client,
    limit: int,
    query_builder: Callable[[NormalizedSeed], str],
    api_fetch: Callable[[Any, str, int], Iterable[Mapping[str, Any]]],
    normalize_records: Callable[[Iterable[Mapping[str, Any]], ArticleNormalizer], list[ArticleEvidence]],
    error_type: type[Exception],
) -> list[ArticleEvidence]:
    records: Iterable[Mapping[str, Any]]
    if fixtures is not None:
        records = fixtures.get(lead.slug, [])[:limit]
    elif client is not None:
        query = query_builder(lead.seed)
        try:
            records = api_fetch(client, query, limit)
        except error_type as exc:
            logger.warning("%s query failed for %s: %s", source.label, lead.company_name, exc)
            return []
    else:
        records = []
    return normalize_records(records, normalizer)


def _update_bundle_metrics(
    *,
    articles_by_source: Mapping[str, Sequence[ArticleEvidence]],
    total_confirming_by_source: dict[str, int],
    unique_domains_global: set[str],
    unique_domains_global_by_source: dict[str, set[str]],
) -> None:
    for source_id, articles in articles_by_source.items():
        confirming = [article for article in articles if article.confirms]
        total_confirming_by_source[source_id] += len(confirming)
        domains = {article.domain for article in confirming if article.domain}
        unique_domains_global_by_source[source_id].update(domains)
        unique_domains_global.update(domains)


def _dedup_articles(articles_by_source: Mapping[str, Sequence[ArticleEvidence]]) -> list[dict[str, str]]:
    articles_all: list[dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for source in VERIFICATION_SOURCES:
        for article in articles_by_source.get(source.id, []):
            if not article.confirms:
                continue
            pair = (article.domain, article.canonical_url)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            articles_all.append({"url": article.url, "domain": article.domain})
    return articles_all


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch_youcom_articles(client, query: str, limit: int) -> Iterable[Mapping[str, Any]]:
    return client.search_news(query=query, limit=limit)


def _fetch_tavily_articles(client, query: str, limit: int) -> Iterable[Mapping[str, Any]]:
    return client.search(query=query, max_results=limit)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv or sys.argv[1:])
    try:
        run_pipeline(
            seed_path=args.seed,
            youcom_path=args.youcom,
            tavily_path=args.tavily,
            output_path=args.output,
            youcom_limit=args.youcom_limit,
            tavily_limit=args.tavily_limit,
        )
    except UnifiedVerifyError as exc:
        logger.error("unified_verify failed: %s (code=%s)", exc, exc.code)
        return 1
    except Exception as exc:  # pragma: no cover - final safeguard
        logger.exception("Unexpected unified_verify failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
