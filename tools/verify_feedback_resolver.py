"""Deterministic feedback resolver that promotes entities from verification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.telemetry import get_telemetry

logger = logging.getLogger("tools.verify_feedback_resolver")

FEEDBACK_VERSION = "v1"
STOPWORDS = {"seed", "round", "series", "funding", "news", "digest", "weekly"}
SPAN_REGEX = re.compile(r"\b([A-Z][A-Za-z0-9&'\.]+(?: [A-Z][A-Za-z0-9&'\.]+){0,2})\b")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _normalize_payload(payload: Any) -> list[dict]:
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, list):
        return payload
    raise ValueError("Unsupported JSON shape.")


def _article_entries(payload: Any) -> list[dict]:
    rows = _normalize_payload(payload)
    entries: list[dict] = []
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
        try:
            payload = load_json(source_path)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {source_path}: {exc}") from exc

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
    best: tuple[str, set[str]] | None = None
    for span, domains in span_domains.items():
        if len(domains) < 2:
            continue
        if best is None:
            best = (span, domains)
            continue
        best_span, best_domains = best
        if len(domains) > len(best_domains):
            best = (span, domains)
        elif len(domains) == len(best_domains):
            span_tokens = len(span.split())
            best_tokens = len(best_span.split())
            if span_tokens < best_tokens or (span_tokens == best_tokens and span.lower() < best_span.lower()):
                best = (span, domains)
    return best


def apply_feedback(
    normalized_path: Path,
    output_path: Path,
    youcom_path: Path,
    tavily_path: Path,
) -> dict[str, Any]:
    payload = load_json(normalized_path)
    rows = _normalize_payload(payload)
    evidence = build_evidence_map(youcom_path, tavily_path)

    feedback_applied = 0
    reviewed = 0
    spans_found = 0
    for row in rows:
        row_id = row.get("id")
        if not row_id or not is_low_confidence(row):
            row["feedback_applied"] = False
            continue

        reviewed += 1
        span_domains = evidence.get(row_id, {})
        spans_found += len(span_domains)
        decision = choose_span(span_domains)
        if not decision:
            row["feedback_applied"] = False
            continue

        span, domains = decision
        original = row.get("company_name")
        if original == span:
            row["feedback_applied"] = False
            continue

        feedback_applied += 1
        reason = f"Entity '{span}' seen in {len(domains)} verified domains"
        payload_hash = hashlib.sha256(
            json.dumps(
                {"id": row_id, "span": span, "domains": sorted(domains)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        row["original_company_name"] = original
        row["company_name"] = span
        row["feedback_applied"] = True
        row["feedback_reason"] = reason
        row["feedback_domains"] = sorted(domains)
        row["feedback_version"] = FEEDBACK_VERSION
        row["feedback_sha256"] = payload_hash

    payload["feedback_version"] = FEEDBACK_VERSION
    payload["feedback_applied"] = feedback_applied
    payload["feedback_sha256"] = hashlib.sha256(
        json.dumps(payload.get("data", rows), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, ensure_ascii=False)
        outfile.write("\n")

    telemetry = get_telemetry()
    telemetry.emit(
        module="feedback_resolver",
        event="summary",
        items_total=len(rows),
        feedback_applied=feedback_applied,
        feedback_candidates_checked=reviewed,
        unique_spans_found=spans_found,
    )
    logger.info("Feedback corrections applied: %s / %s", feedback_applied, len(rows))
    return {"feedback_applied": feedback_applied, "rows_total": len(rows)}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic feedback resolver.")
    parser.add_argument("--input", type=Path, required=True, help="exa_seed.normalized.json input")
    parser.add_argument("--youcom", type=Path, required=True, help="youcom_verified.json")
    parser.add_argument("--tavily", type=Path, required=True, help="tavily_verified.json")
    parser.add_argument("--out", type=Path, required=True, help="Output path for feedback_resolved JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv or [])
    try:
        apply_feedback(
            normalized_path=args.input,
            output_path=args.out,
            youcom_path=args.youcom,
            tavily_path=args.tavily,
        )
    except FileNotFoundError as exc:
        logger.error("INPUT_MISSING_ERROR: %s", exc)
        return 1
    except json.JSONDecodeError as exc:
        logger.error("JSON_PARSE_ERROR: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("Feedback resolver failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
