"""Deterministic confidence scoring from canonical bundle artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pipelines.io.canonical_reader import (
    CanonicalBundle,
    CanonicalReaderError,
    from_bundle_info,
    from_path as load_bundle_from_path,
    load_sources,
)
from pipelines.io.fixture_loader import FixtureArtifactSpec, resolve_bundle_context
from pipelines.news_client import get_runtime_config

logger = logging.getLogger("pipelines.day1.confidence_scoring")

CONFIDENCE_MAP = {
    3: "VERIFIED",
    2: "LIKELY",
}
SOURCE_ORDER = ("exa", "youcom", "tavily")
OUTPUT_SCHEMA_VERSION = 1
DEFAULT_INPUT = Path("leads/tavily_confirmed.json")
DEFAULT_OUTPUT = Path("leads/day1_output.json")
CONFIDENCE_INPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_INPUT, location="leads_dir")
CONFIDENCE_OUTPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_OUTPUT, location="leads_dir")
SENSITIVE_TOKENS = ("key",)


class ConfidenceError(RuntimeError):
    """Domain error for deterministic scoring."""

    def __init__(self, message: str, code: str = "CONF_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ConfidenceRecord:
    """Export-ready confidence payload for a single company."""

    company: str
    confidence: str
    verified_by: list[str]
    proof_links: list[str]
    captured_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "company": self.company,
            "confidence": self.confidence,
            "verified_by": self.verified_by,
            "proof_links": self.proof_links,
            "captured_at": self.captured_at,
        }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI arguments for the deterministic scoring pipeline."""
    parser = argparse.ArgumentParser(
        description="Compute deterministic confidence scores from canonical artifacts.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Path to canonical bundle root (or legacy tavily_confirmed.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination for leads/day1_output.json.",
    )
    parser.add_argument(
        "--ignore-expiry",
        action="store_true",
        help="Allow runs even when the bundle exceeds expiry_days.",
    )
    return parser.parse_args(argv)


def run_pipeline(input_path: Path, output_path: Path, *, ignore_expiry: bool = False) -> list[ConfidenceRecord]:
    """Run deterministic scoring and persist day1_output.json."""
    start = datetime.now(timezone.utc)
    config = get_runtime_config()
    context = resolve_bundle_context(
        config,
        input_path=input_path,
        output_path=output_path,
        input_spec=CONFIDENCE_INPUT_SPEC,
        output_spec=CONFIDENCE_OUTPUT_SPEC,
    )

    bundle = _resolve_bundle(context, context.input_path)
    _enforce_expiry(bundle, ignore_expiry=ignore_expiry)
    logger.info(
        "Confidence scoring start. bundle=%s captured_at=%s input=%s",
        bundle.bundle_id,
        bundle.captured_at.isoformat(),
        bundle.root,
    )

    youcom, tavily, exa = _load_canonical_sources(bundle)
    records = _score_companies(bundle, youcom=youcom, tavily=tavily, exa=exa)
    payload = _build_output_payload(bundle, records)
    output_path = context.output_path
    sha = _persist_output(payload, output_path)
    _log_summary(records, start=start, output_path=output_path, bundle=bundle, sha=sha)
    return records


def _resolve_bundle(context, resolved_input: Path) -> CanonicalBundle:
    if context.bundle:
        return from_bundle_info(context.bundle)
    try:
        return load_bundle_from_path(resolved_input)
    except CanonicalReaderError as exc:
        raise ConfidenceError(f"Unable to load canonical bundle: {exc}", code=exc.code) from exc


def _load_canonical_sources(bundle: CanonicalBundle) -> tuple[list[dict], list[dict], list[dict]]:
    try:
        return load_sources(bundle)
    except CanonicalReaderError as exc:
        raise ConfidenceError(f"Failed to load canonical artifacts: {exc}", code=exc.code) from exc


def _enforce_expiry(bundle: CanonicalBundle, *, ignore_expiry: bool) -> None:
    if not bundle.expiry_days:
        return
    expires_at = bundle.captured_at + timedelta(days=bundle.expiry_days)
    now = datetime.now(timezone.utc)
    if now <= expires_at:
        return
    if ignore_expiry:
        logger.warning(
            "Bundle %s expired on %s; continuing due to --ignore-expiry.",
            bundle.bundle_id,
            expires_at.isoformat(),
        )
        return
    raise ConfidenceError(
        f"Bundle {bundle.bundle_id} expired on {expires_at.isoformat()}",
        code="E_BUNDLE_EXPIRED",
    )


def _score_companies(
    bundle: CanonicalBundle,
    *,
    youcom: Iterable[dict],
    tavily: Iterable[dict],
    exa: Iterable[dict],
) -> list[ConfidenceRecord]:
    captured_at = _format_timestamp(bundle.captured_at)
    company_sources = _index_sources(youcom=youcom, tavily=tavily, exa=exa)
    records: list[ConfidenceRecord] = []
    for company in sorted(company_sources):
        record = _build_confidence_record(
            company=company,
            sources=company_sources[company],
            captured_at=captured_at,
        )
        records.append(record)
    return records


def _index_sources(
    *,
    youcom: Iterable[Mapping[str, object]],
    tavily: Iterable[Mapping[str, object]],
    exa: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, Mapping[str, object]]]:
    indexed: dict[str, dict[str, Mapping[str, object]]] = {}

    def register(source: str, records: Iterable[Mapping[str, object]]) -> None:
        for record in records:
            company = _normalize_company(record.get("company"))
            indexed.setdefault(company, {})[source] = record

    register("youcom", youcom)
    register("tavily", tavily)
    register("exa", exa)
    return indexed


def _build_confidence_record(
    *,
    company: str,
    sources: Mapping[str, Mapping[str, object]],
    captured_at: str,
) -> ConfidenceRecord:
    proof_links, verified_by = _collect_proof_links(sources)
    source_count = len(verified_by)
    confidence = CONFIDENCE_MAP.get(source_count, "EXCLUDE")
    return ConfidenceRecord(
        company=company,
        confidence=confidence,
        verified_by=verified_by,
        proof_links=proof_links,
        captured_at=captured_at,
    )


def _collect_proof_links(sources: Mapping[str, Mapping[str, object]]) -> tuple[list[str], list[str]]:
    proof_links: list[str] = []
    contributors: list[str] = []
    seen: set[tuple[str, str]] = set()

    def add_links(source_name: str, pairs: Iterable[tuple[str | None, str | None]]) -> None:
        added = False
        for publisher, raw_url in pairs:
            sanitized = _sanitize_url(raw_url)
            if not sanitized:
                continue
            key = ((publisher or "").strip().lower(), sanitized)
            if key in seen:
                continue
            seen.add(key)
            proof_links.append(sanitized)
            added = True
        if added:
            contributors.append(source_name)

    if (record := sources.get("youcom")) and record.get("youcom_verified"):
        articles = record.get("press_articles") or []
        publishers = record.get("news_sources") or []
        pairs = []
        for idx, url in enumerate(articles):
            publisher = _publisher_from_index(publishers, idx) or "youcom"
            pairs.append((publisher, url))
        add_links("youcom", pairs)
    if (record := sources.get("tavily")) and record.get("tavily_verified"):
        links = record.get("proof_links") or []
        pairs = [(_publisher_from_url(url), url) for url in links]
        add_links("tavily", pairs)
    if (record := sources.get("exa")) and record.get("source_url"):
        add_links("exa", [("exa", record.get("source_url"))])

    ordered_contributors = [source for source in SOURCE_ORDER if source in contributors]
    return proof_links, ordered_contributors


def _publisher_from_index(publishers: list[str], index: int) -> str | None:
    if not publishers:
        return None
    if 0 <= index < len(publishers):
        return publishers[index]
    return publishers[-1]


def _publisher_from_url(url: str | None) -> str:
    if not url:
        return ""
    netloc = urlparse(url).netloc.lower()
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


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


def _build_output_payload(bundle: CanonicalBundle, records: list[ConfidenceRecord]) -> dict[str, object]:
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "captured_at": _format_timestamp(bundle.captured_at),
        "leads": [record.as_dict() for record in records],
    }


def _persist_output(payload: dict[str, object], output_path: Path) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, sort_keys=True)
        outfile.write("\n")
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return digest


def _log_summary(
    records: Sequence[ConfidenceRecord],
    *,
    start: datetime,
    output_path: Path,
    bundle: CanonicalBundle,
    sha: str,
) -> None:
    counts = Counter(record.confidence for record in records)
    duration = datetime.now(timezone.utc) - start
    logger.info(
        "Confidence summary bundle=%s VERIFIED=%s LIKELY=%s EXCLUDE=%s",
        bundle.bundle_id,
        counts.get("VERIFIED", 0),
        counts.get("LIKELY", 0),
        counts.get("EXCLUDE", 0),
    )
    logger.info(
        "Confidence scoring completed in %.2fs. output=%s sha256=%s",
        duration.total_seconds(),
        output_path,
        sha,
    )


def _normalize_company(value) -> str:
    if value is None:
        return "Unknown"
    return str(value).strip()


def _format_timestamp(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        run_pipeline(args.input, args.output, ignore_expiry=args.ignore_expiry)
    except ConfidenceError as exc:
        logger.error("Confidence scoring failed: %s (code=%s)", exc, exc.code)
        return 1
    except Exception as exc:  # pragma: no cover - safeguard
        logger.exception("Unexpected confidence scoring failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
