"""Deterministic resolver that selects a single company name from candidates."""

from __future__ import annotations

import argparse
import json
import logging
import unicodedata
from dataclasses import dataclass
from datetime import date
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.resolver_rules import ResolverRules, ResolverRulesError, load_rules
from tools.telemetry import get_telemetry

logger = logging.getLogger("tools.resolve_company_name")

RESOLVER_VERSION = "1.0.0"
DEFAULT_INPUT = Path("leads/exa_seed.candidates.json")
DEFAULT_OUTPUT = Path("leads/exa_seed.normalized.json")
DEFAULT_RULES = Path("configs/resolver_rules.v1.yaml")

FUNDING_TOKENS = {"seed", "series", "round", "funding"}
PUBLISHER_TOKENS = {"news", "weekly", "report", "digest", "newsletter", "journal", "gazette"}
VERBS = ("raises", "raised", "secures", "secured", "lands", "landed", "announces", "announced", "bags", "bagged")
DEFAULT_SKIP_REASON = "EMPTY_CANDIDATES_AFTER_FILTER"


class ResolverStats:
    def __init__(self) -> None:
        self.items_total = 0
        self.items_resolved = 0
        self.items_skipped = 0
        self.total_candidates = 0

    def record(self, candidate_count: int, resolved: bool) -> None:
        self.items_total += 1
        if resolved:
            self.items_resolved += 1
        else:
            self.items_skipped += 1
        self.total_candidates += candidate_count

    def metrics(self) -> dict[str, Any]:
        avg = (self.total_candidates / self.items_total) if self.items_total else 0
        return {
            "items_total": self.items_total,
            "items_resolved": self.items_resolved,
            "items_skipped": self.items_skipped,
            "avg_candidates_per_item": round(avg, 2),
        }


@dataclass(frozen=True)
class SeedFields:
    """Normalized seed attributes required for resolution."""

    stage: str
    amount: dict[str, Any]
    source_url: str
    announced_date: str | None


def load_candidates(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as infile:
        payload = json.load(infile)
        if isinstance(payload, Mapping) and "data" in payload:
            return payload
        if isinstance(payload, list):
            return {"data": payload, "items_total": len(payload)}
        raise ResolverRulesError("Resolver input must be a list or an object with data[].", code="INPUT_READ_ERROR")


def write_output(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, ensure_ascii=False)
        outfile.write("\n")


def resolve_company_name(
    *,
    input_path: Path,
    output_path: Path,
    rules: ResolverRules,
) -> Mapping[str, Any]:
    payload = load_candidates(input_path)
    rows: Iterable[Mapping[str, Any]] = payload.get("data", [])
    stats = ResolverStats()
    resolved_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []

    for idx, row in enumerate(rows, start=1):
        result, skip_reason = resolve_row(row, rules)
        stats.record(len(row.get("candidates") or []), result is not None)
        if result is None:
            skipped_rows.append(
                {
                    "id": row.get("id") or f"row_{idx:06d}",
                    "skip_reason": skip_reason or DEFAULT_SKIP_REASON,
                }
            )
            continue
        resolved_rows.append(result)

    metrics = stats.metrics()
    logger.info(
        "Resolver complete version=%s ruleset=%s sha=%s metrics=%s",
        RESOLVER_VERSION,
        rules.version,
        rules.ruleset_sha256,
        metrics,
    )
    telemetry = get_telemetry()
    accuracy_estimate = (
        metrics["items_resolved"] / metrics["items_total"] if metrics["items_total"] else 0
    )
    telemetry.emit(
        module="resolver",
        event="summary",
        resolver_version=RESOLVER_VERSION,
        resolver_ruleset_version=rules.version,
        resolver_ruleset_sha256=rules.ruleset_sha256,
        metrics=metrics,
        accuracy_estimate=round(accuracy_estimate, 3),
    )

    result_payload = {
        "resolver_version": RESOLVER_VERSION,
        "resolver_ruleset_version": rules.version,
        "resolver_ruleset_sha256": rules.ruleset_sha256,
        "items_total": stats.items_total,
        "items_resolved": stats.items_resolved,
        "items_skipped": stats.items_skipped,
        "data": resolved_rows,
        "skipped": skipped_rows,
    }
    write_output(output_path, result_payload)
    return result_payload


def resolve_row(
    row: Mapping[str, Any],
    rules: ResolverRules,
) -> tuple[dict[str, Any] | None, str | None]:
    candidates = row.get("candidates") or []
    if not candidates:
        return None, DEFAULT_SKIP_REASON

    fields, failure_reason = _extract_seed_fields(row)
    if fields is None:
        return None, failure_reason

    raw_title = row.get("raw_title") or ""
    slug_head = extract_slug_head(fields.source_url)
    scores = [score_candidate(candidate, raw_title, slug_head, rules) for candidate in candidates]

    best_idx = choose_candidate(candidates, scores, raw_title, rules)
    if best_idx is None:
        return None, DEFAULT_SKIP_REASON

    chosen = candidates[best_idx]
    resolution_info = {
        "method": f"resolver_{rules.version}",
        "chosen_idx": best_idx,
        "score": scores[best_idx],
        "candidates": candidates,
        "scores": scores,
    }

    resolved = {
        "id": row.get("id"),
        "company_name": chosen,
        "funding_stage": fields.stage,
        "amount": fields.amount,
        "source_url": fields.source_url,
        "raw_title": row.get("raw_title"),
        "raw_snippet": row.get("raw_snippet"),
        "resolution": resolution_info,
        "resolver_ruleset_version": rules.version,
        "resolver_ruleset_sha256": rules.ruleset_sha256,
    }
    if fields.announced_date:
        resolved["announced_date"] = fields.announced_date
    if "generator_ruleset_version" in row:
        resolved["generator_ruleset_version"] = row["generator_ruleset_version"]
    if "generator_ruleset_sha256" in row:
        resolved["generator_ruleset_sha256"] = row["generator_ruleset_sha256"]
    return resolved, None


def _extract_seed_fields(row: Mapping[str, Any]) -> tuple[SeedFields | None, str | None]:
    stage = _normalize_stage(row.get("funding_stage"))
    if not stage:
        return None, "MISSING_STAGE"
    amount = _normalize_amount(row.get("funding_amount"), row.get("funding_currency"))
    if not amount:
        return None, "MISSING_AMOUNT"
    source_url = _normalize_source_url(row.get("source_url"))
    if not source_url:
        return None, "MISSING_SOURCE_URL"
    announced_date = _normalize_announced_date(row.get("announced_date"))
    return SeedFields(stage=stage, amount=amount, source_url=source_url, announced_date=announced_date), None


def score_candidate(candidate: str, title: str, slug_head: str | None, rules: ResolverRules) -> float:
    cleaned = normalize_text(candidate)
    tokens = cleaned.split()
    score = 0.0

    if not any(token in FUNDING_TOKENS for token in map(str.lower, tokens)):
        score += rules.weights.get("no_funding_tokens", 0)
    else:
        score += rules.weights.get("has_funding_token", 0)

    if 1 <= len(tokens) <= 3:
        score += rules.weights.get("token_count_1_3", 0)
    elif len(tokens) >= 5:
        score += rules.weights.get("long_phrase_penalty", 0)

    if any(token[:1].isupper() for token in tokens) or "." in candidate:
        score += rules.weights.get("proper_noun_or_dotted", 0)

    if slug_head:
        distance = levenshtein_distance(cleaned.lower(), slug_head.lower())
        if distance <= rules.slug_head_edit_distance_threshold:
            score += rules.weights.get("close_to_slug_head", 0)

    if appears_near_funding_verb(candidate, title):
        score += rules.weights.get("near_funding_verb", 0)

    if contains_publisher_token(candidate, title):
        score += rules.weights.get("has_publisher_token_or_domain", 0)

    return score


def _normalize_stage(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return None


def _normalize_source_url(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return None


def _normalize_amount(value: Any, currency: Any) -> dict[str, Any] | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    if numeric >= 1_000_000_000:
        scaled = numeric / 1_000_000_000
        unit = "B"
    elif numeric >= 1_000_000:
        scaled = numeric / 1_000_000
        unit = "M"
    else:
        scaled = numeric / 1_000
        unit = "K"
    code = str(currency).strip().upper() if isinstance(currency, str) else "USD"
    code = code or "USD"
    return {"value": round(scaled, 3), "unit": unit, "currency": code}


def _normalize_announced_date(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    # Accept ISO timestamps with optional time component.
    if "T" in candidate:
        candidate = candidate.split("T", 1)[0]
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed.isoformat()


def choose_candidate(candidates: Sequence[str], scores: Sequence[float], title: str, rules: ResolverRules) -> int | None:
    if not candidates:
        return None
    title_lower = (title or "").lower()

    def tie_key(idx: int) -> tuple:
        key_components: list[Any] = []
        for breaker in rules.tie_breakers:
            if breaker == "score_desc":
                key_components.append(-scores[idx])
            elif breaker == "token_count_asc":
                key_components.append(len(normalize_text(candidates[idx]).split()))
            elif breaker == "appears_in_title_first":
                pos = title_lower.find(candidates[idx].lower())
                key_components.append(pos if pos >= 0 else 10_000)
            elif breaker == "lexicographic_ci":
                key_components.append(candidates[idx].casefold())
        return tuple(key_components)

    order = sorted(range(len(candidates)), key=tie_key)
    return order[0] if order else None


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.replace("’", "'").strip()
    text = " ".join(text.split())
    return text


def extract_slug_head(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.strip("/").split("/")
    for segment in reversed(path):
        cleaned = segment.strip()
        if cleaned:
            candidate = cleaned.replace("-", " ")
            normalized = normalize_text(candidate)
            return normalized.split()[0]
    return None


def levenshtein_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        curr = [i]
        for j, char_b in enumerate(b, start=1):
            if char_a == char_b:
                curr.append(prev[j - 1])
            else:
                curr.append(1 + min(prev[j - 1], prev[j], curr[-1]))
        prev = curr
    return prev[-1]


def appears_near_funding_verb(candidate: str, title: str) -> bool:
    if not candidate or not title:
        return False
    title_lower = title.lower()
    candidate_lower = candidate.lower()
    index = title_lower.find(candidate_lower)
    if index == -1:
        return False
    window = title_lower[max(0, index - 40) : index + len(candidate_lower) + 40]
    return any(verb in window for verb in VERBS)


def contains_publisher_token(candidate: str, title: str) -> bool:
    combined = f"{candidate} {title}".lower()
    return any(token in combined for token in PUBLISHER_TOKENS)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve company names from candidates.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to candidates JSON.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Destination for resolved JSON.")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES, help="Resolver rules YAML.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv or [])
    try:
        rules = load_rules(args.rules)
        resolve_company_name(input_path=args.input, output_path=args.output, rules=rules)
    except ResolverRulesError as exc:
        logger.error("%s (code=%s)", exc, exc.code)
        return 1
    except FileNotFoundError as exc:
        logger.error("INPUT_READ_ERROR: %s", exc)
        return 1
    except json.JSONDecodeError as exc:
        logger.error("INPUT_READ_ERROR: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected resolver failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
