"""Deterministic adaptive thresholding for verified leads."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import yaml

from pipelines.io.fixture_loader import FixtureArtifactSpec, resolve_bundle_context
from pipelines.news_client import get_runtime_config

logger = logging.getLogger("pipelines.day1.confidence_scoring_v2")

DEFAULT_INPUT = Path("leads/unified_verify.json")
DEFAULT_OUTPUT = Path("leads/day1_scored.json")
DEFAULT_RULES = Path("configs/verification_rules.v1.yaml")
INPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_INPUT, location="leads_dir")
OUTPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_OUTPUT, location="leads_dir")
RULES_VERSION_OVERRIDE_ENV = "RULES_VERSION_OVERRIDE"
TIMESTAMP_ENV = "FUND_SIGNAL_BUNDLE_TIMESTAMP"
SENSITIVE_TOKENS = ("key",)


class ScoringError(RuntimeError):
    """Raised when deterministic scoring fails."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceDescriptor:
    """Configured source metadata."""

    id: str
    label: str


@dataclass(frozen=True)
class ScoreWeights:
    """Point values for each heuristic."""

    mainstream_domain_pair: int
    normalized_field_match: int
    dual_source_confirmation: int


@dataclass(frozen=True)
class ScoreThresholds:
    """Thresholds that map points to labels."""

    verified: int
    likely: int


@dataclass(frozen=True)
class Ruleset:
    """Parsed scoring ruleset."""

    version: str
    sha256: str
    weights: ScoreWeights
    thresholds: ScoreThresholds
    mainstream_domains: frozenset[str]
    discovery_sources: tuple[SourceDescriptor, ...]
    verification_sources: tuple[SourceDescriptor, ...]


@dataclass(frozen=True)
class ArticleMatch:
    """Normalized confirmation article details."""

    url: str | None
    domain: str | None
    amount_match: bool
    stage_match: bool

    @property
    def is_confirming(self) -> bool:
        return self.amount_match or self.stage_match


@dataclass(frozen=True)
class NormalizedFields:
    """Subset of normalized lead data needed for scoring."""

    stage: str | None
    amount_value: int | float | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> NormalizedFields:
        if not isinstance(payload, Mapping):
            payload = {}
        return cls(stage=_extract_stage(payload), amount_value=_extract_amount_value(payload))

    def missing_field_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.stage:
            warnings.append("missing_normalized_stage")
        if self.amount_value is None:
            warnings.append("missing_normalized_amount")
        return warnings


@dataclass(frozen=True)
class ScoredLead:
    """Final scored lead payload."""

    lead_id: str
    company_name: str
    confidence_points: int
    final_label: str
    verified_by: list[str]
    proof_links: list[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.lead_id,
            "company_name": self.company_name,
            "confidence_points": self.confidence_points,
            "final_label": self.final_label,
            "verified_by": self.verified_by,
            "proof_links": self.proof_links,
            "warnings": self.warnings,
        }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic adaptive confidence scoring.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Unified verification JSON input")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES, help="Path to ruleset YAML file")
    parser.add_argument(
        "--output",
        "--out",
        dest="output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination for day1_scored.json",
    )
    parser.add_argument(
        "--timestamp",
        type=str,
        default=None,
        help="Override timestamp (ISO 8601) written to scored_at; falls back to bundle capture time.",
    )
    return parser.parse_args(argv)


def run_pipeline(
    input_path: Path,
    rules_path: Path,
    output_path: Path,
    *,
    timestamp_override: str | None = None,
) -> list[ScoredLead]:
    """Execute deterministic scoring."""

    start = datetime.now(UTC)
    config = get_runtime_config()
    context = resolve_bundle_context(
        config,
        input_path=input_path,
        output_path=output_path,
        input_spec=INPUT_SPEC,
        output_spec=OUTPUT_SPEC,
    )

    ruleset = _load_ruleset(rules_path)
    leads = _load_leads(context.input_path)
    scored = _score_leads(leads, ruleset)

    scored_at = _resolve_output_timestamp(timestamp_override, context.scoring_timestamp or start)
    payload = _build_output_payload(
        scored,
        ruleset,
        scored_at=scored_at,
    )
    sha = _persist_output(payload, context.output_path)
    _log_summary(scored, start=start, output_path=context.output_path, sha=sha)
    return scored


def _load_ruleset(path: Path) -> Ruleset:
    if not path.exists():
        raise ScoringError(f"Ruleset missing at {path}", code="RULES_MISSING")
    blob = path.read_bytes()
    try:
        raw = yaml.safe_load(blob.decode("utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - unlikely but surfaces quickly
        raise ScoringError(f"Unable to parse ruleset: {exc}", code="SCHEMA_INVALID") from exc

    if not isinstance(raw, Mapping):
        raise ScoringError("Ruleset must be a mapping", code="SCHEMA_INVALID")

    version = str(raw.get("version") or "").strip()
    if not version:
        raise ScoringError("Ruleset missing version", code="SCHEMA_INVALID")

    thresholds_raw = raw.get("thresholds") or {}
    weights_raw = raw.get("weights") or {}
    mainstream_raw = raw.get("mainstream_domains") or []
    sources_raw = raw.get("sources") or {}

    thresholds = ScoreThresholds(
        verified=int(thresholds_raw.get("verified", 3)),
        likely=int(thresholds_raw.get("likely", 2)),
    )
    weights = ScoreWeights(
        mainstream_domain_pair=int(weights_raw.get("mainstream_domain_pair", 2)),
        normalized_field_match=int(weights_raw.get("normalized_field_match", 1)),
        dual_source_confirmation=int(weights_raw.get("dual_source_confirmation", 1)),
    )

    mainstream_domains = frozenset(_normalize_domain(item) for item in mainstream_raw if item)
    discovery_sources = _build_source_descriptors(sources_raw.get("discovery"), default=(SourceDescriptor(id="exa", label="Exa"),))
    verification_sources = _build_source_descriptors(
        sources_raw.get("verification"),
        default=(
            SourceDescriptor(id="youcom", label="You.com"),
            SourceDescriptor(id="tavily", label="Tavily"),
        ),
    )

    version_override = os.getenv(RULES_VERSION_OVERRIDE_ENV)
    if version_override:
        logger.info("RULES_VERSION_OVERRIDE set: %s -> %s", version, version_override)
        version = version_override

    sha256 = hashlib.sha256(blob).hexdigest()
    return Ruleset(
        version=version,
        sha256=sha256,
        weights=weights,
        thresholds=thresholds,
        mainstream_domains=mainstream_domains,
        discovery_sources=discovery_sources,
        verification_sources=verification_sources,
    )


def _load_leads(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ScoringError(f"Input not found: {path}", code="SCHEMA_INVALID")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScoringError(f"Invalid JSON: {exc}", code="SCHEMA_INVALID") from exc

    leads: list[dict[str, Any]]
    if isinstance(payload, list):
        leads = payload
    elif isinstance(payload, Mapping):
        for key in ("leads", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                leads = value
                break
        else:
            raise ScoringError("Input payload missing leads array", code="SCHEMA_INVALID")
    else:
        raise ScoringError("Unsupported input format", code="SCHEMA_INVALID")

    normalized: list[dict[str, Any]] = []
    for entry in leads:
        if not isinstance(entry, Mapping):
            raise ScoringError("Lead entries must be objects", code="SCHEMA_INVALID")
        normalized.append(dict(entry))
    return normalized


def _score_leads(leads: Sequence[Mapping[str, Any]], ruleset: Ruleset) -> list[ScoredLead]:
    scored: list[ScoredLead] = []
    for lead in sorted(leads, key=_sort_key):
        scored.append(_score_single_lead(lead, ruleset))
    return scored


def _score_single_lead(raw: Mapping[str, Any], ruleset: Ruleset) -> ScoredLead:
    lead_id, company_name = _lead_identity(raw)
    normalized_fields = NormalizedFields.from_payload(raw.get("normalized"))
    articles_by_source, article_warnings = _articles_from_confirmations(raw.get("confirmations"), ruleset.verification_sources)
    confirming_articles = _confirming_articles(articles_by_source)
    points = _calculate_points(confirming_articles, articles_by_source, normalized_fields, ruleset)
    label = _label_for_points(points, ruleset.thresholds)
    verified_by = _collect_verified_by(ruleset.discovery_sources, ruleset.verification_sources, articles_by_source)
    proof_links = _collect_proof_links(ruleset.verification_sources, articles_by_source)

    return ScoredLead(
        lead_id=lead_id,
        company_name=company_name,
        confidence_points=points,
        final_label=label,
        verified_by=verified_by,
        proof_links=proof_links,
        warnings=_dedupe_preserve_order([*normalized_fields.missing_field_warnings(), *article_warnings]),
    )


def _collect_proof_links(
    order: Sequence[SourceDescriptor],
    articles_by_source: Mapping[str, Sequence[ArticleMatch]],
) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for source in order:
        for article in articles_by_source.get(source.id, []):
            if not article.is_confirming:
                continue
            sanitized = _sanitize_url(article.url)
            if not sanitized or sanitized in seen:
                continue
            seen.add(sanitized)
            links.append(sanitized)
    return links


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _label_for_points(points: int, thresholds: ScoreThresholds) -> str:
    if points >= thresholds.verified:
        return "VERIFIED"
    if points >= thresholds.likely:
        return "LIKELY"
    return "EXCLUDE"


def _build_output_payload(
    leads: Sequence[ScoredLead],
    ruleset: Ruleset,
    *,
    scored_at: datetime,
) -> dict[str, Any]:
    return {
        "ruleset_version": ruleset.version,
        "ruleset_sha256": ruleset.sha256,
        "scored_at": _format_timestamp(scored_at),
        "leads": [lead.as_dict() for lead in leads],
    }


def _persist_output(payload: Mapping[str, Any], output_path: Path) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, sort_keys=False)
        outfile.write("\n")
    return hashlib.sha256(output_path.read_bytes()).hexdigest()


def _log_summary(
    leads: Sequence[ScoredLead],
    *,
    start: datetime,
    output_path: Path,
    sha: str,
) -> None:
    counts: dict[str, int] = {"VERIFIED": 0, "LIKELY": 0, "EXCLUDE": 0}
    for lead in leads:
        counts[lead.final_label] = counts.get(lead.final_label, 0) + 1
    duration = datetime.now(UTC) - start
    logger.info(
        "Confidence scoring complete: VERIFIED=%s LIKELY=%s EXCLUDE=%s",
        counts.get("VERIFIED", 0),
        counts.get("LIKELY", 0),
        counts.get("EXCLUDE", 0),
    )
    logger.info("Output written to %s (sha256=%s) in %.2fs", output_path, sha, duration.total_seconds())


def _sort_key(lead: Mapping[str, Any]) -> tuple[str, str]:
    lead_id = str(lead.get("id") or lead.get("lead_id") or "").strip()
    company = str(lead.get("company_name") or lead.get("company") or "").strip().lower()
    return (lead_id, company)


def _extract_stage(normalized: Mapping[str, Any]) -> str | None:
    for key in ("stage", "funding_stage"):
        value = normalized.get(key)
        if value:
            return str(value)
    return None


def _extract_amount_value(normalized: Mapping[str, Any]) -> int | float | None:
    amount = normalized.get("amount") or normalized.get("funding_amount")
    if isinstance(amount, Mapping):
        value = amount.get("value")
        if value is not None:
            return value
    if isinstance(amount, int | float):
        return amount
    return None


def _build_source_descriptors(entries: Any, *, default: tuple[SourceDescriptor, ...]) -> tuple[SourceDescriptor, ...]:
    if not isinstance(entries, list):
        return default
    results: list[SourceDescriptor] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        identifier = str(entry.get("id") or "").strip()
        label = str(entry.get("label") or "").strip()
        if not identifier or not label:
            continue
        results.append(SourceDescriptor(id=identifier, label=label))
    return tuple(results) or default


def _lead_identity(raw: Mapping[str, Any]) -> tuple[str, str]:
    lead_id = str(raw.get("id") or raw.get("lead_id") or "").strip() or "unknown"
    company_name = str(raw.get("company_name") or raw.get("company") or "Unknown").strip() or "Unknown"
    return lead_id, company_name


def _articles_from_confirmations(
    confirmations: Any,
    verification_sources: Sequence[SourceDescriptor],
) -> tuple[dict[str, list[ArticleMatch]], list[str]]:
    if isinstance(confirmations, Mapping):
        normalized_confirmations = {str(key).lower(): value for key, value in confirmations.items()}
    else:
        normalized_confirmations = {}

    warnings: list[str] = []
    articles_by_source: dict[str, list[ArticleMatch]] = {}
    for source in verification_sources:
        raw_records = _normalize_confirmation_records(normalized_confirmations.get(source.id.lower()))
        if not raw_records:
            warnings.append(f"missing_{source.id}_confirmations")
            articles_by_source[source.id] = []
            continue

        normalized_records = [_build_article_match(record) for record in raw_records]
        if not any(article.is_confirming for article in normalized_records):
            warnings.append(f"no_confirming_{source.id}_articles")
        articles_by_source[source.id] = normalized_records
    return articles_by_source, warnings


def _normalize_confirmation_records(records: Any) -> list[Mapping[str, Any]]:
    if isinstance(records, list):
        return [record for record in records if isinstance(record, Mapping)]
    if isinstance(records, Mapping):
        return [records]
    return []


def _build_article_match(record: Mapping[str, Any]) -> ArticleMatch:
    match_block = record.get("match") or {}
    if not isinstance(match_block, Mapping):
        match_block = {}
    url_value = str(record.get("url") or record.get("source_url") or record.get("link") or "").strip() or None
    domain_value = _normalize_domain(record.get("domain") or record.get("publisher") or record.get("url"))
    return ArticleMatch(
        url=url_value,
        domain=domain_value,
        amount_match=bool(match_block.get("amount")),
        stage_match=bool(match_block.get("stage")),
    )


def _confirming_articles(articles_by_source: Mapping[str, Sequence[ArticleMatch]]) -> list[ArticleMatch]:
    return [article for articles in articles_by_source.values() for article in articles if article.is_confirming]


def _calculate_points(
    confirming_articles: Sequence[ArticleMatch],
    articles_by_source: Mapping[str, Sequence[ArticleMatch]],
    normalized_fields: NormalizedFields,
    ruleset: Ruleset,
) -> int:
    points = 0

    mainstream_domains = {
        article.domain
        for article in confirming_articles
        if article.domain and article.domain in ruleset.mainstream_domains
    }
    if len(mainstream_domains) >= 2:
        points += ruleset.weights.mainstream_domain_pair

    if _has_normalized_field_match(normalized_fields, confirming_articles):
        points += ruleset.weights.normalized_field_match

    if _all_sources_confirmed(articles_by_source, ruleset.verification_sources):
        points += ruleset.weights.dual_source_confirmation

    return points


def _has_normalized_field_match(fields: NormalizedFields, confirming_articles: Sequence[ArticleMatch]) -> bool:
    if not confirming_articles:
        return False
    matches_amount = fields.amount_value is not None and any(article.amount_match for article in confirming_articles)
    matches_stage = bool(fields.stage) and any(article.stage_match for article in confirming_articles)
    return matches_amount or matches_stage


def _all_sources_confirmed(
    articles_by_source: Mapping[str, Sequence[ArticleMatch]],
    verification_sources: Sequence[SourceDescriptor],
) -> bool:
    if not verification_sources:
        return False
    return all(_has_confirming_articles(articles_by_source.get(source.id, ())) for source in verification_sources)


def _has_confirming_articles(articles: Sequence[ArticleMatch] | None) -> bool:
    return any(article.is_confirming for article in articles or ())


def _collect_verified_by(
    discovery_sources: Sequence[SourceDescriptor],
    verification_sources: Sequence[SourceDescriptor],
    articles_by_source: Mapping[str, Sequence[ArticleMatch]],
) -> list[str]:
    verified_by = [descriptor.label for descriptor in discovery_sources]
    for source in verification_sources:
        if _has_confirming_articles(articles_by_source.get(source.id, ())):
            verified_by.append(source.label)
    return verified_by


def _normalize_domain(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "//" in value else f"https://{value}")
    host = parsed.netloc or parsed.path
    host = host.lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _sanitize_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(token in key.lower() for token in SENSITIVE_TOKENS)
    ]
    sanitized = parsed._replace(query=urlencode(filtered_query, doseq=True))
    return urlunparse(sanitized)


def _format_timestamp(value: datetime) -> str:
    value = value.astimezone(UTC)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_output_timestamp(cli_value: str | None, fallback: datetime) -> datetime:
    raw = cli_value or os.getenv(TIMESTAMP_ENV)
    if not raw:
        return fallback
    try:
        return _parse_timestamp(raw)
    except ValueError as exc:  # pragma: no cover - invalid override text
        raise ScoringError(f"Invalid timestamp override: {raw}", code="INVALID_TIMESTAMP") from exc


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise ValueError("timestamp cannot be empty")
    normalized = normalized.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    try:
        run_pipeline(args.input, args.rules, args.output, timestamp_override=args.timestamp)
    except ScoringError as exc:
        logger.error("confidence_scoring_v2 failed: %s (code=%s)", exc, exc.code)
        return 1
    except Exception as exc:  # pragma: no cover - safeguard
        logger.exception("Unexpected confidence scoring failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
