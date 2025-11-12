"""Deterministic feedback resolver that promotes entities from verification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.manifest_utils import compute_sha256, update_manifest
from tools.telemetry import get_telemetry

logger = logging.getLogger("tools.verify_feedback_resolver")

FEEDBACK_VERSION = "v1"
STOPWORDS = {"seed", "round", "series", "funding", "news", "digest", "weekly"}
SPAN_REGEX = re.compile(r"\b([A-Z][A-Za-z0-9&'\.]+(?: [A-Z][A-Za-z0-9&'\.]+){0,2})\b")


@dataclass
class FeedbackMetrics:
    total_rows: int
    reviewed: int = 0
    spans_found: int = 0
    applied: int = 0

    def record_review(self, span_domains_count: int) -> None:
        self.reviewed += 1
        self.spans_found += span_domains_count

    def record_application(self) -> None:
        self.applied += 1


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _normalize_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, list):
        return payload
    raise ValueError("Unsupported JSON shape.")


def _relative_to_manifest(target: Path, manifest_path: Path) -> str:
    try:
        return str(target.relative_to(manifest_path.parent))
    except ValueError as exc:
        raise ValueError(f"Output path {target} is outside manifest root {manifest_path.parent}") from exc


def _canonical_data_sha(rows: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        rows,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _article_entries(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = _normalize_payload(payload)
    entries: list[dict[str, Any]] = []
    for row in rows:
        articles = row.get("articles") or row.get("press_articles") or []
        for article in articles:
            entries.append(
                {
                    "id": row.get("id") or row.get("company") or row.get("source_url"),
                    "title": article.get("title") or "",
                    "snippet": article.get("snippet") or "",
                    "url": article.get("url") or article.get("source_url") or "",
                    "domain": article.get("domain"),
                }
            )
    return entries


def _extract_domain(url: str, fallback: str | None = None) -> str | None:
    if fallback:
        return fallback.lower()
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _extract_spans(text: str) -> list[str]:
    results: list[str] = []
    for match in SPAN_REGEX.finditer(text or ""):
        value = match.group(1).strip()
        if not value:
            continue
        lower = value.lower()
        if lower in STOPWORDS:
            continue
        results.append(value)
    return results


def build_evidence_map(youcom_path: Path, tavily_path: Path) -> dict[str, dict[str, set[str]]]:
    evidence: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for source_path in (youcom_path, tavily_path):
        if not source_path.exists():
            continue
        payload = _safe_load_json(source_path)

        for article in _article_entries(payload):
            row_id = article["id"]
            if not row_id:
                continue
            domain = _extract_domain(article.get("url"), article.get("domain"))
            if not domain:
                continue
            spans = _extract_spans(f"{article.get('title','')} {article.get('snippet','')}")
            for span in spans:
                evidence[row_id][span].add(domain)
    return evidence


def _safe_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {path}: {exc}") from exc


def is_low_confidence(row: Mapping[str, Any]) -> bool:
    resolution = row.get("resolution") or {}
    label = str(resolution.get("final_label") or "").upper()
    if label == "EXCLUDE":
        return True
    score = resolution.get("score")
    if isinstance(score, int | float) and score < 2:
        return True
    return False


def choose_span(span_domains: Mapping[str, set[str]]) -> tuple[str, set[str]] | None:
    candidates = [
        (span, domains)
        for span, domains in span_domains.items()
        if len(domains) >= 2
    ]
    if not candidates:
        return None
    return min(candidates, key=_span_rank)


def _span_rank(entry: tuple[str, set[str]]) -> tuple[int, int, str]:
    span, domains = entry
    # Use negatives so min() prefers higher domain count, fewer tokens, then lexicographic ascending
    return (-len(domains), len(span.split()), span.lower() or "")


def apply_feedback(
    normalized_path: Path,
    output_path: Path,
    youcom_path: Path,
    tavily_path: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    payload = load_json(normalized_path)
    rows = _normalize_payload(payload)
    evidence = build_evidence_map(youcom_path, tavily_path)

    metrics = FeedbackMetrics(total_rows=len(rows))

    for row in rows:
        row_id = row.get("id")
        if not row_id or not is_low_confidence(row):
            row["feedback_applied"] = False
            continue

        span_domains = evidence.get(row_id, {})
        metrics.record_review(len(span_domains))
        decision = choose_span(span_domains)
        if not decision:
            row["feedback_applied"] = False
            continue

        span, domains = decision
        original = row.get("company_name")
        if original == span:
            row["feedback_applied"] = False
            continue

        metrics.record_application()
        _apply_span_feedback(row=row, span=span, domains=domains)

    payload["feedback_version"] = FEEDBACK_VERSION
    payload["feedback_applied"] = metrics.applied
    payload_rows = payload.get("data", rows) if isinstance(payload, Mapping) else rows
    payload["feedback_sha256"] = _canonical_data_sha(payload_rows)

    _write_json(output_path, payload)
    output_sha256 = compute_sha256(output_path)

    if manifest_path:
        rel_path = _relative_to_manifest(output_path, manifest_path)
        update_manifest(manifest_path, {rel_path: output_sha256})

    telemetry = get_telemetry()
    telemetry.emit(
        module="feedback_resolver",
        event="summary",
        items_total=metrics.total_rows,
        feedback_applied=metrics.applied,
        feedback_candidates_checked=metrics.reviewed,
        unique_spans_found=metrics.spans_found,
        feedback_sha256=payload["feedback_sha256"],
    )
    logger.info(
        "Feedback corrections applied: %s / %s (output_sha=%s)",
        metrics.applied,
        len(rows),
        output_sha256,
    )
    return {
        "feedback_applied": metrics.applied,
        "rows_total": len(rows),
        "output_sha256": output_sha256,
    }


def _apply_span_feedback(*, row: dict[str, Any], span: str, domains: set[str]) -> None:
    row["original_company_name"] = row.get("company_name")
    row["company_name"] = span
    row["feedback_applied"] = True
    row["feedback_reason"] = f"Entity '{span}' seen in {len(domains)} verified domains"
    sorted_domains = sorted(domains)
    row["feedback_domains"] = sorted_domains
    row["feedback_version"] = FEEDBACK_VERSION
    row["feedback_sha256"] = _row_feedback_hash(row_id=row.get("id"), span=span, domains=sorted_domains)


def _row_feedback_hash(*, row_id: Any, span: str, domains: Sequence[str]) -> str:
    digest_input = json.dumps(
        {"id": row_id, "span": span, "domains": list(domains)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, ensure_ascii=False)
        outfile.write("\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic feedback resolver.")
    parser.add_argument("--input", type=Path, required=True, help="exa_seed.normalized.json input")
    parser.add_argument("--youcom", type=Path, required=True, help="youcom_verified.json")
    parser.add_argument("--tavily", type=Path, required=True, help="tavily_verified.json")
    parser.add_argument("--out", type=Path, required=True, help="Output path for feedback_resolved JSON")
    parser.add_argument(
        "--update-manifest",
        type=Path,
        default=None,
        help="Optional manifest.json to update with the feedback_resolved SHA entry.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        apply_feedback(
            normalized_path=args.input,
            output_path=args.out,
            youcom_path=args.youcom,
            tavily_path=args.tavily,
            manifest_path=args.update_manifest,
        )
    except FileNotFoundError as exc:
        logger.error("INPUT_MISSING_ERROR: %s", exc)
        return 1
    except ValueError as exc:
        logger.error("INPUT_VALIDATION_ERROR: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("Feedback resolver failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
