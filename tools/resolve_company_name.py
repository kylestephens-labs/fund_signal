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
DEFAULT_RULES = Path("configs/resolver_rules.v1.1.yaml")

FUNDING_TOKENS = {"seed", "series", "round", "funding"}
PUBLISHER_TOKENS = {"news", "weekly", "report", "digest", "newsletter", "journal", "gazette", "daily", "times"}
PUBLISHER_PREFIXES = {"the", "der", "die", "das", "la", "le", "los", "las", "el"}
PUBLISHER_PREFIX_COMBINATIONS = {
    f"{prefix} {token}" for prefix in PUBLISHER_PREFIXES for token in ("news", "daily", "digest", "report")
}
VERBS = ("raises", "raised", "secures", "secured", "lands", "landed", "announces", "announced", "bags", "bagged")
VERB_STARTERS = VERBS + (
    "receives",
    "received",
    "receiving",
    "wins",
    "won",
    "acquires",
    "acquired",
    "acquiring",
    "earns",
    "earning",
)
LOCALE_FUNDING_VERBS = (
    "erhält",
    "erhaelt",
    "erhalten",
    "obtiene",
    "obtuvo",
    "obtient",
    "obtiennent",
    "recauda",
    "recaudo",
    "recaudó",
    "levanta",
    "levanto",
    "consegue",
    "conseguiu",
)
GERUND_SUFFIXES = ("ing", "ando", "iendo")
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


@dataclass(frozen=True)
class CandidateView:
    """Preprocessed view of a candidate string for scoring/tie-breaking."""

    raw: str
    normalized: str
    normalized_lower: str
    tokens: tuple[str, ...]
    raw_lower: str
    meta: Mapping[str, Any] | None = None

    @classmethod
    def from_raw(cls, value: str, meta: Mapping[str, Any] | None = None) -> CandidateView:
        normalized = normalize_text(value)
        return cls(
            raw=value,
            normalized=normalized,
            normalized_lower=normalized.lower(),
            tokens=tuple(normalized.split()),
            raw_lower=value.lower() if value else "",
            meta=meta or {},
        )

    @property
    def token_count(self) -> int:
        return len(self.tokens)


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

    raw_title_value = row.get("raw_title")
    title_for_scoring = raw_title_value if isinstance(raw_title_value, str) else ""
    title_lower = title_for_scoring.lower()
    slug_head = extract_slug_head(fields.source_url)
    slug_head_lower = slug_head.lower() if slug_head else None
    candidate_feature_map = row.get("candidate_features") or {}
    candidate_views = [
        CandidateView.from_raw(candidate, candidate_feature_map.get(candidate))
        for candidate in candidates
    ]
    score_results = [
        score_candidate(candidate_view, title_lower, slug_head_lower, rules)
        for candidate_view in candidate_views
    ]
    scores = [result[0] for result in score_results]
    feature_details = [
        {"candidate": view.raw, "signals": result[1]}
        for view, result in zip(candidate_views, score_results, strict=False)
    ]

    best_idx = choose_candidate(candidate_views, scores, title_lower, rules)
    if best_idx is None:
        return None, DEFAULT_SKIP_REASON

    chosen = candidates[best_idx]
    resolution_info = {
        "method": f"resolver_{rules.version}",
        "chosen_idx": best_idx,
        "score": scores[best_idx],
        "candidates": candidates,
        "scores": scores,
        "feature_flags": feature_details,
    }

    resolved = {
        "id": row.get("id"),
        "company_name": chosen,
        "funding_stage": fields.stage,
        "amount": fields.amount,
        "source_url": fields.source_url,
        "raw_title": raw_title_value,
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


def score_candidate(
    candidate: CandidateView,
    title_lower: str,
    slug_head_lower: str | None,
    rules: ResolverRules,
) -> tuple[float, dict[str, Any]]:
    signals = compute_candidate_signals(candidate, title_lower, slug_head_lower, rules)
    score = 0.0
    for feature_name, weight in rules.weights.items():
        if weight == 0:
            continue
        value = _feature_scalar(signals.get(feature_name))
        if not value:
            continue
        score += weight * value
    return score, signals


def compute_candidate_signals(
    candidate: CandidateView,
    title_lower: str,
    slug_head_lower: str | None,
    rules: ResolverRules,
) -> dict[str, Any]:
    signals: dict[str, Any] = {}
    lower_tokens = [token.lower() for token in candidate.tokens]
    has_funding_token = any(token in FUNDING_TOKENS for token in lower_tokens)
    signals["has_funding_token"] = has_funding_token
    signals["no_funding_tokens"] = not has_funding_token
    signals["token_count_1_3"] = 1 <= candidate.token_count <= 3
    signals["long_phrase_penalty"] = candidate.token_count >= 5
    signals["proper_noun_or_dotted"] = any(token[:1].isupper() for token in candidate.tokens) or "." in candidate.raw

    if slug_head_lower:
        distance = levenshtein_distance(candidate.normalized_lower, slug_head_lower)
        signals["slug_head_edit_distance"] = distance
        signals["close_to_slug_head"] = distance <= rules.slug_head_edit_distance_threshold
        proximity = max(0, (rules.slug_head_edit_distance_threshold + 1) - distance)
        signals["slug_head_proximity_bonus"] = proximity if proximity > 0 else 0
    else:
        signals["slug_head_edit_distance"] = None
        signals["close_to_slug_head"] = False
        signals["slug_head_proximity_bonus"] = 0

    signals["near_funding_verb"] = appears_near_funding_verb(candidate.raw_lower, title_lower)
    signals["locale_verb_hit"] = appears_near_locale_verb(candidate.raw_lower, title_lower)
    signals["has_publisher_token_or_domain"] = contains_publisher_token(candidate.raw_lower, title_lower)
    signals["has_publisher_prefix"] = _has_publisher_prefix(candidate)
    signals["starts_with_verb_or_gerund"] = _starts_with_verb_or_gerund(candidate)
    signals["possessive_plural_repaired"] = bool((candidate.meta or {}).get("possessive_plural_repaired"))
    return signals


def _feature_scalar(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _has_publisher_prefix(candidate: CandidateView) -> bool:
    if candidate.token_count == 0:
        return False
    first = candidate.tokens[0].lower()
    second = candidate.tokens[1].lower() if candidate.token_count > 1 else ""
    third = candidate.tokens[2].lower() if candidate.token_count > 2 else ""
    if first in PUBLISHER_PREFIXES and (
        second in PUBLISHER_TOKENS or third in PUBLISHER_TOKENS or second == "news"
    ):
        return True
    if first in PUBLISHER_TOKENS:
        return True
    combined = f"{first} {second}".strip()
    return combined in PUBLISHER_PREFIX_COMBINATIONS


def _starts_with_verb_or_gerund(candidate: CandidateView) -> bool:
    if candidate.token_count == 0:
        return False
    first = candidate.tokens[0].lower()
    if first in VERB_STARTERS or first in LOCALE_FUNDING_VERBS:
        return True
    return any(first.endswith(suffix) for suffix in GERUND_SUFFIXES)


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


def choose_candidate(
    candidates: Sequence[CandidateView],
    scores: Sequence[float],
    title_lower: str,
    rules: ResolverRules,
) -> int | None:
    if not candidates:
        return None
    if not scores:
        return None

    def tie_key(idx: int) -> tuple[Any, ...]:
        key_components: list[Any] = []
        candidate = candidates[idx]
        for breaker in rules.tie_breakers:
            if breaker == "score_desc":
                key_components.append(-scores[idx])
            elif breaker == "token_count_asc":
                key_components.append(candidate.token_count)
            elif breaker == "appears_in_title_first":
                pos = title_lower.find(candidate.raw_lower)
                key_components.append(pos if pos >= 0 else 10_000)
            elif breaker == "lexicographic_ci":
                key_components.append(candidate.raw.casefold())
        return tuple(key_components)

    try:
        return min(range(len(candidates)), key=tie_key)
    except ValueError:
        return None


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


def appears_near_funding_verb(candidate_lower: str, title_lower: str) -> bool:
    return _appears_near_keywords(candidate_lower, title_lower, VERBS)


def appears_near_locale_verb(candidate_lower: str, title_lower: str) -> bool:
    return _appears_near_keywords(candidate_lower, title_lower, LOCALE_FUNDING_VERBS)


def _appears_near_keywords(candidate_lower: str, title_lower: str, keywords: Sequence[str]) -> bool:
    if not candidate_lower or not title_lower:
        return False
    index = title_lower.find(candidate_lower)
    if index == -1:
        return False
    window = title_lower[max(0, index - 40) : index + len(candidate_lower) + 40]
    return any(keyword in window for keyword in keywords)


def contains_publisher_token(candidate_lower: str, title_lower: str) -> bool:
    if not candidate_lower and not title_lower:
        return False
    combined = f"{candidate_lower} {title_lower}"
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
