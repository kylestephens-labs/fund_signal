"""Normalize noisy Exa seed items into deterministic tuples."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from pipelines.io.schemas import FundingAmount, NormalizedSeed

logger = logging.getLogger("tools.normalize_exa_seed")

SKIP_MISSING_SOURCE = "MISSING_SOURCE_URL"
SKIP_MISSING_COMPANY = "MISSING_COMPANY"
SKIP_INVALID_AMOUNT = "INVALID_AMOUNT"

NORMALIZER_VERSION = "1.0.0"
COMPANY_VERBS = (
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
)
COMPANY_REGEX = re.compile(
    rf"(?P<company>[A-Z][A-Za-z0-9&'().\-]+(?: [A-Z][A-Za-z0-9&'().\-]+){{0,4}})\s+(?:{'|'.join(COMPANY_VERBS)})",
    re.IGNORECASE,
)
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

    def record_total(self) -> None:
        self.items_total += 1

    def record_parsed(self, normalized: NormalizedSeed) -> None:
        self.items_parsed += 1
        self.coverage_by_field["company_name"] += int(bool(normalized.company_name))
        self.coverage_by_field["funding_stage"] += int(bool(normalized.funding_stage))
        self.coverage_by_field["amount"] += 1
        self.coverage_by_field["announced_date"] += int(normalized.announced_date is not None)

    def record_skipped(self) -> None:
        self.items_skipped += 1


def _join_text(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


class SeedNormalizer:
    """Regex + heuristic based normalization."""

    def normalize(self, record: Mapping[str, Any]) -> tuple[NormalizedSeed | None, str | None]:
        raw_title = self._coalesce_text(record, "company", "title")
        raw_snippet = self._optional_text(record, "snippet", "summary")
        source_url = record.get("source_url")
        if not source_url:
            return None, SKIP_MISSING_SOURCE

        company = self._extract_company(raw_title, raw_snippet, source_url)
        if not company:
            return None, SKIP_MISSING_COMPANY

        funding_stage = self._extract_stage(record.get("funding_stage"), raw_title, raw_snippet)
        amount = self._extract_amount(record.get("funding_amount"), raw_title, raw_snippet)
        if amount is None:
            return None, SKIP_INVALID_AMOUNT

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
        return normalized, None

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

    def _extract_company(self, title: str, snippet: str | None, url: str | None) -> str | None:
        haystack = _join_text(title, snippet)
        if title and self._looks_like_company(title):
            cleaned = self._clean_company(title)
            if cleaned:
                return cleaned
        match = COMPANY_REGEX.search(haystack)
        if match:
            return self._clean_company(match.group("company"))

        for delimiter in (":", "|", " – ", " - "):
            if delimiter in title:
                tail = title.split(delimiter, 1)[-1].strip()
                candidate_match = re.match(r"([A-Z][A-Za-z0-9&'().\-]+(?: [A-Z][A-Za-z0-9&'().\-]+){0,3})", tail)
                if candidate_match:
                    cleaned = self._clean_company(candidate_match.group(0))
                    if cleaned:
                        return cleaned

        if url:
            parsed = urlparse(url)
            host = parsed.netloc.split(".")
            if host:
                candidate = host[0].replace("-", " ").strip()
                if candidate:
                    return candidate.title()
        return None

    @staticmethod
    def _clean_company(value: str) -> str | None:
        cleaned = re.sub(r"[\"“”]", "", value).strip()
        return cleaned or None

    @staticmethod
    def _looks_like_company(value: str) -> bool:
        if any(token in value.lower() for token in ("series", "seed", "|", "round", "funding")):
            return False
        return bool(re.match(r"^[A-Za-z0-9&'().\-\s]+$", value)) and len(value.split()) <= 4

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


def normalize_records(records: Iterable[Mapping[str, Any]], normalizer: SeedNormalizer | None = None) -> dict:
    normalizer = normalizer or SeedNormalizer()
    stats = NormalizationStats()
    data: list[dict] = []
    skipped: list[dict] = []

    for idx, record in enumerate(records, start=1):
        stats.record_total()
        normalized, error = normalizer.normalize(record)
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
        data.append(normalized.model_dump(mode="json"))

    return {
        "normalizer_version": NORMALIZER_VERSION,
        "items_total": stats.items_total,
        "items_parsed": stats.items_parsed,
        "items_skipped": stats.items_skipped,
        "coverage_by_field": stats.coverage_by_field,
        "data": data,
        "skipped": skipped,
    }


def normalize_file(input_path: Path, output_path: Path) -> dict:
    records = load_records(input_path)
    payload = normalize_records(records)
    write_output(output_path, payload)
    logger.info(
        "Normalization complete. items_total=%s parsed=%s skipped=%s coverage=%s",
        payload["items_total"],
        payload["items_parsed"],
        payload["items_skipped"],
        payload["coverage_by_field"],
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        normalize_file(args.input, args.output)
    except FileNotFoundError as exc:
        logger.error("Input file not found: %s", exc)
        return 1
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse input JSON: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
