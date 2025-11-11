"""Normalize noisy Exa seed items into deterministic tuples."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from pipelines.io.schemas import FundingAmount, NormalizedSeed

logger = logging.getLogger("tools.normalize_exa_seed")

SKIP_MISSING_SOURCE = "MISSING_SOURCE_URL"
SKIP_MISSING_COMPANY = "MISSING_COMPANY"
SKIP_INVALID_AMOUNT = "INVALID_AMOUNT"

NORMALIZER_VERSION = "2.0.0"
DEFAULT_RULES_PATH = Path("configs/normalizer_rules.v1.yaml")
DEFAULT_VERBS = (
    "raises",
    "raised",
    "secures",
    "secured",
    "snags",
    "snagged",
    "lands",
    "landed",
    "closes",
    "closed",
    "bags",
    "bagged",
    "nabs",
    "nabbed",
    "hauls",
    "hauled",
    "announces",
    "announced",
)
@dataclass(frozen=True)
class NormalizerRules:
    """Versioned heuristics for company extraction."""

    version: str
    sha256: str
    verbs: tuple[str, ...]
    delimiters: tuple[str, ...]
    publisher_keywords: tuple[str, ...]
    publisher_domains: tuple[str, ...]
    slug_stopwords: tuple[str, ...]
    company_domain_exceptions: tuple[str, ...]
    bad_company_tokens: tuple[str, ...]
    publisher_delimiter_direction: Mapping[str, str]


def _build_company_regex(verbs: Sequence[str]) -> re.Pattern[str]:
    cleaned = [verb.strip().lower() for verb in verbs if verb and verb.strip()]
    token = "|".join(re.escape(verb) for verb in cleaned) or "raises"
    pattern = rf"(?P<company>[A-Z][A-Za-z0-9&'().\-]+(?: [A-Z][A-Za-z0-9&'().\-]+){{0,4}})\s+(?:{token})"
    return re.compile(pattern, re.IGNORECASE)


STAGE_REGEX = re.compile(
    r"(pre-seed|seed|series [a-e]|series [a-e]\s+round|series\s+[a-e][\+\-]?)",
    re.IGNORECASE,
)
AMOUNT_REGEX = re.compile(
    r"(?P<currency>\$|€|£|usd|eur|gbp)?\s*(?P<value>\d{1,3}(?:[,\d]{0,3})(?:\.\d+)?)\s*(?P<unit>billion|million|thousand|bn|mm|m|b|k)?",
    re.IGNORECASE,
)
DATE_REGEX = re.compile(
    r"((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
CURRENCY_MAP = {
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}

_RULE_CACHE: dict[Path, NormalizerRules] = {}


def load_rules(path: Path | None = None) -> NormalizerRules:
    """Load and cache normalizer rules from YAML."""
    target = (path or DEFAULT_RULES_PATH).expanduser()
    resolved = target.resolve()
    cached = _RULE_CACHE.get(resolved)
    if cached:
        return cached
    if not resolved.exists():
        raise FileNotFoundError(f"Normalizer rules missing at {resolved}")
    blob = resolved.read_bytes()
    sha256 = hashlib.sha256(blob).hexdigest()
    raw = yaml.safe_load(blob.decode("utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"Normalizer rules must be a mapping (got {type(raw).__name__})")
    version = str(raw.get("version") or "v1").strip()
    verbs = _as_tuple(raw.get("verbs"), fallback=DEFAULT_VERBS)
    delimiters = _as_tuple(raw.get("delimiters"), fallback=(" | ", " - ", " — ", " – ", ": "))
    publisher_keywords = _as_tuple(raw.get("publisher_keywords"))
    publisher_domains = _as_tuple(raw.get("publisher_domains"))
    slug_stopwords = _as_tuple(raw.get("slug_stopwords"))
    company_domain_exceptions = _as_tuple(raw.get("company_domain_exceptions"))
    bad_company_tokens = _as_tuple(raw.get("bad_company_tokens"))
    delimiter_direction_raw = raw.get("publisher_delimiter_direction") or {}
    delimiter_direction = {str(key).lower(): str(value).lower() for key, value in delimiter_direction_raw.items()}
    rules = NormalizerRules(
        version=version,
        sha256=sha256,
        verbs=verbs,
        delimiters=delimiters,
        publisher_keywords=publisher_keywords,
        publisher_domains=publisher_domains,
        slug_stopwords=slug_stopwords,
        company_domain_exceptions=company_domain_exceptions,
        bad_company_tokens=bad_company_tokens,
        publisher_delimiter_direction=delimiter_direction,
    )
    _RULE_CACHE[resolved] = rules
    return rules


def _as_tuple(value: Any, *, fallback: Sequence[str] = ()) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Sequence):
        items = [item for item in value if isinstance(item, str)]
    else:
        items = list(fallback)
    cleaned = []
    for item in items:
        stripped = item.strip()
        if stripped:
            cleaned.append(stripped)
    if not cleaned and fallback:
        cleaned = list(fallback)
    return tuple(cleaned)


@dataclass
class NormalizationStats:
    items_total: int = 0
    items_parsed: int = 0
    items_skipped: int = 0
    coverage_by_field: dict[str, int] = field(
        default_factory=lambda: {
            "company_name": 0,
            "funding_stage": 0,
            "amount": 0,
            "announced_date": 0,
        }
    )
    publisher_flagged: int = 0
    publisher_split_used: int = 0
    url_slug_used: int = 0
    final_accepted: int = 0

    def record_total(self) -> None:
        self.items_total += 1

    def record_parsed(self, normalized: NormalizedSeed) -> None:
        self.items_parsed += 1
        self.coverage_by_field["company_name"] += int(bool(normalized.company_name))
        self.coverage_by_field["funding_stage"] += int(bool(normalized.funding_stage))
        self.coverage_by_field["amount"] += 1
        self.coverage_by_field["announced_date"] += int(normalized.announced_date is not None)
        self.final_accepted += 1

    def record_skipped(self) -> None:
        self.items_skipped += 1

    def record_extraction(self, meta: Mapping[str, Any] | None) -> None:
        if not meta:
            return
        if meta.get("publisher_flagged"):
            self.publisher_flagged += 1
        if meta.get("publisher_split_used"):
            self.publisher_split_used += 1
        if meta.get("url_slug_used"):
            self.url_slug_used += 1

    def metrics(self) -> dict[str, int]:
        return {
            "publisher_flagged": self.publisher_flagged,
            "publisher_split_used": self.publisher_split_used,
            "url_slug_used": self.url_slug_used,
            "final_accepted": self.final_accepted,
        }


def _join_text(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


class SeedNormalizer:
    """Regex + heuristic based normalization."""

    def __init__(self, rules: NormalizerRules | None = None) -> None:
        self.rules = rules or load_rules()
        self._company_regex = _build_company_regex(self.rules.verbs)

    def normalize(self, record: Mapping[str, Any]) -> tuple[NormalizedSeed | None, str | None, dict[str, Any]]:
        meta = self._default_meta()
        raw_title = self._coalesce_text(record, "company", "title")
        raw_snippet = self._optional_text(record, "snippet", "summary")
        source_url = record.get("source_url")
        if not source_url:
            return None, SKIP_MISSING_SOURCE, meta

        company_candidates = self._extract_company(raw_title, raw_snippet, source_url, meta)
        company = self._select_candidate(company_candidates, source_url, meta)
        if not company:
            return None, SKIP_MISSING_COMPANY, meta

        funding_stage = self._extract_stage(record.get("funding_stage"), raw_title, raw_snippet)
        amount = self._extract_amount(record.get("funding_amount"), raw_title, raw_snippet)
        if amount is None:
            return None, SKIP_INVALID_AMOUNT, meta

        announced_date = self._extract_date(record.get("funding_date"), raw_title, raw_snippet)
        normalized = NormalizedSeed(
            company_name=company,
            funding_stage=funding_stage,
            amount=amount,
            announced_date=announced_date,
            source_url=source_url,
            raw_title=raw_title or None,
            raw_snippet=raw_snippet,
        )
        return normalized, None, meta

    @staticmethod
    def _default_meta() -> dict[str, Any]:
        return {
            "extraction_method": "regex",
            "publisher_flagged": False,
            "publisher_split_used": False,
            "url_slug_used": False,
        }

    @staticmethod
    def _coalesce_text(record: Mapping[str, Any], *keys: str) -> str:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    return stripped
        return ""

    def _optional_text(self, record: Mapping[str, Any], *keys: str) -> str | None:
        value = self._coalesce_text(record, *keys)
        return value or None

    def _extract_company(
        self,
        title: str,
        snippet: str | None,
        url: str | None,
        meta: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        haystack = _join_text(title, snippet)
        if title and self._looks_like_company(title):
            cleaned = self._clean_company(title)
            if cleaned:
                candidates.append({"value": cleaned, "reason": "exact_title", "score": 3})
        match = self._company_regex.search(haystack)
        if match:
            candidate = self._clean_company(match.group("company"))
            if candidate:
                if self._is_publisher_candidate(candidate, title, url, check_domain=True):
                    meta["publisher_flagged"] = True
                    split_candidates = self._extract_from_delimiters(title, url)
                    if split_candidates:
                        meta["publisher_split_used"] = True
                        meta["extraction_method"] = "publisher_split"
                        candidates.extend(split_candidates)
                else:
                    candidates.append({"value": candidate, "reason": "regex", "score": 2})

        if not meta.get("publisher_split_used"):
            split_candidates = self._extract_from_delimiters(title, url)
            if split_candidates:
                meta["publisher_split_used"] = True
                meta["extraction_method"] = "publisher_split"
                candidates.extend(split_candidates)

        slug_candidate = self._extract_from_slug(url)
        if slug_candidate:
            meta["url_slug_used"] = True
            candidates.append({"value": slug_candidate, "reason": "url_slug", "score": 1})

        return candidates

    @staticmethod
    def _clean_company(value: str) -> str | None:
        cleaned = re.sub(r"[\"“”]", "", value).strip()
        return cleaned or None

    @staticmethod
    def _looks_like_company(value: str) -> bool:
        lower = value.lower()
        if any(token in lower for token in ("series", "seed", "|", "round", "funding")):
            return False
        if lower.startswith("www"):
            return False
        if re.search(r"[a-z0-9]+\.[a-z]{2,}", lower):
            return False
        return bool(re.match(r"^[A-Za-z0-9&'().\-\s]+$", value)) and len(value.split()) <= 4

    def _is_publisher_candidate(
        self,
        candidate: str | None,
        context: str,
        url: str | None,
        *,
        check_domain: bool = True,
    ) -> bool:
        lowered = (candidate or "").lower()
        text = f"{context.lower()} {lowered}".strip()
        if any(keyword in text for keyword in self.rules.publisher_keywords):
            return True
        if check_domain:
            domain = self._normalized_domain(url)
            if domain and domain in self.rules.publisher_domains:
                return True
        if lowered.startswith("www") or re.fullmatch(r"[a-z0-9\-]+\.[a-z]{2,}", lowered):
            return lowered not in self.rules.company_domain_exceptions
        return False

    def _extract_from_delimiters(self, title: str, url: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        direction = self._delimiter_direction(url)
        for delimiter in self.rules.delimiters:
            if delimiter in title:
                segments = [segment.strip() for segment in title.split(delimiter) if segment.strip()]
                ordered = segments if direction == "left" else segments[::-1]
                for segment in ordered:
                    match = self._company_regex.search(segment)
                    if match:
                        cleaned = self._clean_company(match.group("company"))
                        if cleaned and self._looks_like_company(cleaned):
                            results.append({"value": cleaned, "reason": "delimiter_regex", "score": 2})
                for segment in ordered:
                    cleaned = self._clean_company(segment)
                    if cleaned and self._looks_like_company(cleaned):
                        results.append({"value": cleaned, "reason": "delimiter_plain", "score": 1})
        return results

    def _delimiter_direction(self, url: str | None) -> str:
        domain = self._normalized_domain(url)
        return self.rules.publisher_delimiter_direction.get(domain, self.rules.publisher_delimiter_direction.get("default", "left"))

    def _extract_from_slug(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        best = ""
        for segment in segments[::-1]:
            cleaned = re.sub(r"[\d_]+", "", segment).strip("-")
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered in self.rules.slug_stopwords:
                continue
            verb_split = self._split_on_verbs(lowered)
            candidate = verb_split or lowered
            if candidate in self.rules.slug_stopwords:
                continue
            best = candidate
            break
        if not best:
            host = parsed.netloc.split(".")
            if host:
                return host[0].replace("-", " ").strip().title()
            return None
        if "." in best and best.count(".") == 1:
            return best.lower()
        tokens = [token for token in best.replace("-", " ").split() if token and token not in self.rules.slug_stopwords]
        if not tokens:
            return None
        if len(tokens) <= 3:
            return " ".join(token.capitalize() for token in tokens)
        return " ".join(tokens[:3]).title()

    def _split_on_verbs(self, text: str) -> str | None:
        for verb in self.rules.verbs:
            pattern = f"-{verb}"
            if pattern in text:
                return text.split(pattern, 1)[0].strip("-")
        return None

    def _select_candidate(self, candidates: list[dict[str, Any]], url: str | None, meta: dict[str, Any]) -> str | None:
        filtered = []
        for candidate in candidates:
            value = candidate.get("value")
            if not value:
                continue
            cleaned = self._normalize_candidate_text(value)
            if cleaned in self.rules.bad_company_tokens:
                continue
            if cleaned in self.rules.slug_stopwords:
                continue
            if self._is_publisher_candidate(value, "", url, check_domain=False):
                continue
            tokens = value.split()
            if len(tokens) > 6:
                continue
            filtered.append(candidate)
        if not filtered:
            return None
        filtered.sort(
            key=lambda entry: (
                -entry.get("score", 0),
                len(entry.get("value", "").split()),
                entry.get("value", "").lower(),
            )
        )
        best = filtered[0]
        meta["extraction_method"] = best.get("reason", "regex")
        return best.get("value")

    @staticmethod
    def _normalized_domain(url: str | None) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host

    def _extract_stage(self, stage_value: str | None, title: str, snippet: str | None) -> str:
        if stage_value:
            return self._normalize_stage(stage_value)
        haystack = _join_text(title, snippet)
        match = STAGE_REGEX.search(haystack)
        if match:
            return self._normalize_stage(match.group(0))
        return "Seed"

    @staticmethod
    def _normalize_stage(stage: str) -> str:
        value = stage.strip().lower()
        if "pre-seed" in value:
            return "Pre-Seed"
        if value.startswith("seed"):
            return "Seed"
        match = re.match(r"series\s+([a-e])", value)
        if match:
            return f"Series {match.group(1).upper()}"
        return stage.strip().title()

    @staticmethod
    def _normalize_candidate_text(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip()).lower()

    def _extract_amount(self, amount_value: int | float | None, title: str, snippet: str | None) -> FundingAmount | None:
        if amount_value and amount_value > 0:
            return FundingAmount(
                value=self._scaled_value(amount_value),
                unit=self._unit_from_amount(amount_value),
                currency="USD",
            )

        haystack = _join_text(title, snippet)
        match = AMOUNT_REGEX.search(haystack)
        if not match:
            return None
        currency_token = (match.group("currency") or "").lower()
        currency = CURRENCY_MAP.get(currency_token, "USD")
        raw_value = match.group("value").replace(",", "")
        try:
            value = float(raw_value)
        except ValueError:
            return None
        unit_token = (match.group("unit") or "").lower()
        if unit_token in {"b", "bn", "billion"}:
            unit = "B"
        elif unit_token in {"m", "mm", "million"}:
            unit = "M"
        else:
            unit = "K" if unit_token in {"k", "thousand"} else "M"
        return FundingAmount(value=round(value, 3), unit=unit, currency=currency)

    @staticmethod
    def _scaled_value(amount: int | float) -> float:
        if amount >= 1_000_000_000:
            return round(amount / 1_000_000_000, 3)
        if amount >= 1_000_000:
            return round(amount / 1_000_000, 3)
        return round(amount / 1_000, 3)

    @staticmethod
    def _unit_from_amount(amount: int | float) -> str:
        if amount >= 1_000_000_000:
            return "B"
        if amount >= 1_000_000:
            return "M"
        return "K"

    def _extract_date(self, date_value: str | None, title: str, snippet: str | None):
        if date_value:
            parsed = self._parse_iso_date(date_value)
            if parsed:
                return parsed
        haystack = _join_text(title, snippet)
        match = DATE_REGEX.search(haystack)
        if match:
            parsed_text = self._parse_month_day_year(match.group(0))
            if parsed_text:
                return parsed_text
        return None

    @staticmethod
    def _parse_iso_date(value: str) -> date | None:
        candidate = value.rstrip("Z")
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_month_day_year(value: str) -> date | None:
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None


def load_records(input_path: Path) -> list[dict]:
    opener = gzip.open if input_path.suffix == ".gz" else open
    with opener(input_path, "rt", encoding="utf-8") as handle:
        peek = handle.read(1024)
        handle.seek(0)
        if peek.lstrip().startswith("["):
            return json.load(handle)
        return [json.loads(line) for line in handle if line.strip()]


def write_output(output_path: Path, payload: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, default=str)
        outfile.write("\n")


def normalize_records(
    records: Iterable[Mapping[str, Any]],
    normalizer: SeedNormalizer | None = None,
    rules: NormalizerRules | None = None,
) -> dict:
    if normalizer is None:
        active_rules = rules or load_rules()
        normalizer = SeedNormalizer(active_rules)
    else:
        active_rules = normalizer.rules
    stats = NormalizationStats()
    data: list[dict] = []
    skipped: list[dict] = []

    for idx, record in enumerate(records, start=1):
        stats.record_total()
        normalized, error, meta = normalizer.normalize(record)
        if normalized is None:
            stats.record_skipped()
            skipped.append(
                {
                    "line_number": idx,
                    "skip_reason": error,
                    "raw_title": (record.get("company") or record.get("title") or "")[:80],
                }
            )
            logger.info("PARSE_ERROR line=%s reason=%s", idx, error)
            continue
        stats.record_parsed(normalized)
        stats.record_extraction(meta)
        row = normalized.model_dump(mode="json")
        if meta:
            row["extraction_method"] = meta.get("extraction_method", "regex")
        data.append(row)

    return {
        "normalizer_version": NORMALIZER_VERSION,
        "ruleset_version": active_rules.version,
        "ruleset_sha256": active_rules.sha256,
        "items_total": stats.items_total,
        "items_parsed": stats.items_parsed,
        "items_skipped": stats.items_skipped,
        "coverage_by_field": stats.coverage_by_field,
        "metrics": stats.metrics(),
        "data": data,
        "skipped": skipped,
    }


def normalize_file(input_path: Path, output_path: Path, *, rules_path: Path | None = None) -> dict:
    records = load_records(input_path)
    rules = load_rules(rules_path)
    payload = normalize_records(records, rules=rules)
    write_output(output_path, payload)
    logger.info(
        "Normalization complete. items_total=%s parsed=%s skipped=%s coverage=%s ruleset=%s",
        payload["items_total"],
        payload["items_parsed"],
        payload["items_skipped"],
        payload["coverage_by_field"],
        payload["ruleset_version"],
    )
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize Exa seed JSON.")
    parser.add_argument("--input", type=Path, default=Path("leads/exa_seed.json"), help="Path to Exa seed JSON/JSONL(.gz).")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("leads/exa_seed.normalized.json"),
        help="Destination for normalized JSON payload.",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES_PATH,
        help="Path to normalizer rules YAML (defaults to configs/normalizer_rules.v1.yaml).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        normalize_file(args.input, args.output, rules_path=args.rules)
    except FileNotFoundError as exc:
        logger.error("Input file not found: %s", exc)
        return 1
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse input JSON: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
