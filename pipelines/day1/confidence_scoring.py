"""Confidence scoring pipeline for Day 1 lead consolidation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from operator import attrgetter
from pathlib import Path
from typing import Callable, Iterable, Sequence

from app.models.lead import CompanyFunding
from pipelines.io.fixture_loader import BundleInfo, FixtureArtifactSpec, resolve_bundle_context
from pipelines.io.manifest_loader import build_freshness_metadata
from pipelines.news_client import get_runtime_config

logger = logging.getLogger("pipelines.day1.confidence_scoring")


CONFIDENCE_LEVELS = {
    3: "VERIFIED",
    2: "LIKELY",
    1: "EXCLUDE",
    0: "EXCLUDE",
}
CONFIDENCE_DISPLAY = {
    "VERIFIED": "HIGH",
    "LIKELY": "MEDIUM",
    "EXCLUDE": "LOW",
}
EXPORTABLE_LABELS = {"VERIFIED", "LIKELY"}
DEFAULT_INPUT = Path("leads/tavily_confirmed.json")
DEFAULT_OUTPUT = Path("leads/day1_output.json")
CONFIDENCE_INPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_INPUT, location="leads_dir")
CONFIDENCE_OUTPUT_SPEC = FixtureArtifactSpec(default_path=DEFAULT_OUTPUT, location="leads_dir")

SourcePredicate = Callable[[CompanyFunding], bool]
SOURCE_PREDICATES: tuple[tuple[str, SourcePredicate], ...] = (
    ("Exa", attrgetter("exa_found")),
    ("You.com", attrgetter("youcom_verified")),
    ("Tavily", attrgetter("tavily_verified")),
)


class ConfidenceError(RuntimeError):
    """Domain error for scoring pipeline."""

    def __init__(self, message: str, code: str = "CONF_ERROR") -> None:
        super().__init__(message)
        self.code = code


def load_leads(path: Path) -> list[CompanyFunding]:
    """Load funding leads from JSON."""
    if not path.exists():
        raise ConfidenceError(f"Input file does not exist: {path}", code="CONF_INPUT_ERR")
    with path.open("r", encoding="utf-8") as infile:
        payload = json.load(infile)
    if not isinstance(payload, list):
        raise ConfidenceError("Input JSON must be a list.", code="CONF_INPUT_ERR")
    try:
        leads = [CompanyFunding.model_validate(item) for item in payload]
    except Exception as exc:  # pragma: no cover - validation safety
        raise ConfidenceError(f"Failed to parse input records: {exc}", code="CONF_INPUT_ERR") from exc
    return leads


def persist_leads(leads: list[CompanyFunding], path: Path) -> None:
    """Persist leads to JSON."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as outfile:
            json.dump([lead.model_dump(mode="json") for lead in leads], outfile, indent=2)
            outfile.write("\n")
    except Exception as exc:  # pragma: no cover - I/O safety
        raise ConfidenceError(f"Failed to write output file: {exc}", code="CONF_EXPORT_ERR") from exc


def compute_verified_by(lead: CompanyFunding) -> list[str]:
    """Return the list of sources that verified the lead."""
    return [
        label
        for label, predicate in SOURCE_PREDICATES
        if predicate(lead)
    ]


def compute_confidence(num_sources: int) -> str:
    """Compute confidence label based on number of confirming sources."""
    return CONFIDENCE_LEVELS.get(num_sources, "EXCLUDE")


def build_watermark(lead: CompanyFunding, *, now: datetime | None = None) -> str:
    """Construct the freshness watermark string."""
    verified_by = ", ".join(lead.verified_by) if lead.verified_by else "None"
    confidence_label = lead.confidence or "EXCLUDE"
    confidence_desc = CONFIDENCE_DISPLAY.get(confidence_label, "LOW")
    last_checked_dt = lead.last_checked_at or now or datetime.now(tz=timezone.utc)
    last_checked = last_checked_dt.astimezone(timezone.utc).strftime("%b %d, %Y")
    return f"Verified by: {verified_by} • Last checked: {last_checked} • Confidence: {confidence_desc}"


def enrich_lead(lead: CompanyFunding, *, timestamp: datetime) -> CompanyFunding:
    """Populate confidence metadata on the lead."""
    if lead is None:
        raise ConfidenceError("Lead is None.", code="CONF_INPUT_ERR")
    verified_by = compute_verified_by(lead)
    if not verified_by:
        raise ConfidenceError(
            f"No verification sources present for {lead.company}.",
            code="CONF_INPUT_ERR",
        )

    lead.verified_by = verified_by
    lead.confidence = compute_confidence(len(verified_by))
    lead.last_checked_at = timestamp
    lead.freshness_watermark = build_watermark(lead, now=timestamp)
    return lead


def score_leads(leads: list[CompanyFunding], timestamp: datetime | None = None) -> list[CompanyFunding]:
    """Apply confidence scoring to loaded leads."""
    timestamp = timestamp or datetime.now(tz=timezone.utc)
    scored: list[CompanyFunding] = []
    for lead in leads:
        try:
            enriched = enrich_lead(lead, timestamp=timestamp)
        except ConfidenceError as exc:
            logger.error("Skipping lead due to error: %s (code=%s)", exc, exc.code)
            continue
        scored.append(enriched)
    return scored


def filter_exportable(leads: Sequence[CompanyFunding]) -> list[CompanyFunding]:
    """Return only VERIFIED or LIKELY leads."""
    return [lead for lead in leads if lead.confidence in EXPORTABLE_LABELS]


def summarize(leads: Iterable[CompanyFunding]) -> None:
    """Emit summary counts for observability."""
    counter = Counter(
        (lead.confidence or "EXCLUDE") for lead in leads
    )
    logger.info(
        "Confidence summary: VERIFIED=%s LIKELY=%s EXCLUDE=%s",
        counter.get("VERIFIED", 0),
        counter.get("LIKELY", 0),
        counter.get("EXCLUDE", 0),
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Score Day 1 leads for confidence and freshness.")
    parser.add_argument("--input", type=Path, default=Path("leads/tavily_confirmed.json"), help="Input JSON file.")
    parser.add_argument("--output", type=Path, default=Path("leads/day1_output.json"), help="Output JSON file.")
    return parser.parse_args(argv)


def _apply_bundle_freshness(leads: Sequence[CompanyFunding], bundle: BundleInfo) -> None:
    freshness = build_freshness_metadata(bundle.bundle_id, bundle.captured_at, bundle.expiry_days)
    logger.info("Freshness watermark: %s", freshness.watermark)
    if freshness.warning:
        logger.warning("⚠️ Bundle nearing expiry (age %.2fd / %sd).", freshness.age_days, bundle.expiry_days)
    for lead in leads:
        lead.last_checked_at = freshness.captured_at
        lead.freshness_watermark = freshness.watermark


def run_pipeline(input_path: Path, output_path: Path) -> list[CompanyFunding]:
    """Run the confidence scoring pipeline end-to-end."""
    config = get_runtime_config()
    context = resolve_bundle_context(
        config,
        input_path=input_path,
        output_path=output_path,
        input_spec=CONFIDENCE_INPUT_SPEC,
        output_spec=CONFIDENCE_OUTPUT_SPEC,
    )

    leads = load_leads(context.input_path)
    logger.info("Loaded %s leads from %s.", len(leads), context.input_path)
    scored = score_leads(leads, timestamp=context.scoring_timestamp)
    summarize(scored)
    exportable = filter_exportable(scored)
    if context.bundle:
        _apply_bundle_freshness(exportable, context.bundle)
    logger.info("Exporting %s leads (VERIFIED/LIKELY) to %s.", len(exportable), context.output_path)
    persist_leads(exportable, context.output_path)
    return exportable


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for confidence scoring."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv or sys.argv[1:])
    try:
        run_pipeline(args.input, args.output)
    except ConfidenceError as exc:
        logger.error("Confidence scoring failed: %s (code=%s)", exc, exc.code)
        return 1
    except Exception as exc:  # pragma: no cover - safeguard
        logger.exception("Unexpected error during confidence scoring: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
