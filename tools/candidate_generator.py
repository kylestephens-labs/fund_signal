"""Generate multiple deterministic company-name candidates for Exa seeds."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.normalize_exa_seed import NormalizerRules, load_rules
from tools.telemetry import get_telemetry

logger = logging.getLogger("tools.candidate_generator")

GENERATOR_VERSION = "1.0.0"
DEFAULT_INPUT = Path("leads/exa_seed.json")
DEFAULT_OUTPUT = Path("leads/exa_seed.candidates.json")
DEFAULT_RULES = Path("configs/normalizer_rules.v1.yaml")


@dataclass
class CandidateStats:
    items_total: int = 0
    items_with_candidates: int = 0
    publisher_flagged: int = 0
    publisher_split_used: int = 0
    url_slug_used: int = 0
    total_candidates: int = 0

    def record(self, features: Mapping[str, Any], candidate_count: int) -> None:
        self.items_total += 1
        if candidate_count:
            self.items_with_candidates += 1
        if features.get("publisher_flagged"):
            self.publisher_flagged += 1
        if features.get("publisher_split_used"):
            self.publisher_split_used += 1
        if features.get("url_slug_used"):
            self.url_slug_used += 1
        self.total_candidates += candidate_count

    def metrics(self) -> dict[str, float]:
        avg = (self.total_candidates / self.items_total) if self.items_total else 0.0
        return {
            "items_total": self.items_total,
            "items_with_candidates": self.items_with_candidates,
            "publisher_flagged": self.publisher_flagged,
            "publisher_split_used": self.publisher_split_used,
            "url_slug_used": self.url_slug_used,
            "avg_candidates_per_item": round(avg, 2),
        }


def load_records(input_path: Path) -> list[Mapping[str, Any]]:
    opener = gzip.open if input_path.suffix == ".gz" else open
    with opener(input_path, "rt", encoding="utf-8") as handle:
        peek = handle.read(1024)
        handle.seek(0)
        if peek.lstrip().startswith("["):
            data = json.load(handle)
            if isinstance(data, list):
                return data
            raise ValueError("Input JSON array expected.")
        return [json.loads(line) for line in handle if line.strip()]


def write_output(output_path: Path, payload: Mapping[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, ensure_ascii=False)
        outfile.write("\n")


class CandidateGenerator:
    """Generate candidate company names from titles, delimiters, and slugs."""

    METHOD_PRIORITY = {
        "url_slug": 4,
        "title_regex": 3,
        "delimiter_regex": 2,
        "delimiter_plain": 1,
        "raw_title": 0,
    }

    def __init__(self, rules: NormalizerRules) -> None:
        self.rules = rules
        self._regex = self._build_regex(rules.verbs)

    def generate(self, record: Mapping[str, Any], *, row_index: int) -> tuple[dict[str, Any] | None, str | None]:
        title = (record.get("title") or record.get("company") or "").strip()
        snippet = (record.get("snippet") or record.get("summary") or "").strip()
        url = record.get("url") or record.get("source_url") or ""
        if not title and not url:
            return None, "ROW_PARSE_ERROR"

        features = {
            "publisher_flagged": False,
            "publisher_split_used": False,
            "url_slug_used": False,
        }
        raw_candidates = self._extract_candidates(title, snippet, url, features)
        normalized_candidates, methods, candidate_features = self._normalize_candidates(raw_candidates)

        if not normalized_candidates:
            return None, "EMPTY_CANDIDATES_AFTER_NORMALIZATION"

        row_id = str(
            record.get("id")
            or record.get("lead_id")
            or record.get("uuid")
            or f"row_{row_index:06d}"
        )
        payload = {
            "id": row_id,
            "raw_title": title or None,
            "raw_snippet": snippet or None,
            "source_url": url or None,
            "features": features,
            "candidates": normalized_candidates,
            "extraction_methods": methods,
            "candidate_features": candidate_features,
        }
        payload.update(_seed_metadata(record))
        return payload, None

    def _extract_candidates(
        self,
        title: str,
        snippet: str,
        url: str,
        features: dict[str, Any],
    ) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        haystack = self._join_text(title, snippet)

        if title:
            candidates.append((title, "raw_title"))

        match = self._regex.search(haystack)
        if match:
            candidate = match.group("company")
            candidates.append((candidate, "title_regex"))
            if self._is_publisher_phrase(candidate, title, url):
                features["publisher_flagged"] = True

        delimiter_candidates = self._extract_from_delimiters(title, url)
        if delimiter_candidates:
            features["publisher_split_used"] = True
            candidates.extend(delimiter_candidates)

        slug_candidate = self._extract_from_slug(url)
        if slug_candidate:
            features["url_slug_used"] = True
            candidates.append((slug_candidate, "url_slug"))

        return candidates

    def _extract_from_delimiters(self, title: str, url: str) -> list[tuple[str, str]]:
        if not title:
            return []
        direction = self._delimiter_direction(url)
        segments: list[str] = [title]
        for delimiter in self.rules.delimiters:
            if delimiter in title:
                parts = [segment.strip() for segment in title.split(delimiter) if segment.strip()]
                segments = parts if direction == "left" else parts[::-1]
                break

        results: list[tuple[str, str]] = []
        for segment in segments:
            if not segment:
                continue
            regex_match = self._regex.search(segment)
            if regex_match:
                results.append((regex_match.group("company"), "delimiter_regex"))
            else:
                results.append((segment, "delimiter_plain"))
        return results

    def _extract_from_slug(self, url: str) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        for segment in reversed(segments):
            cleaned = re.sub(r"[\d_]+", "", segment).strip("-")
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered in self.rules.slug_stopwords:
                continue
            trimmed = self._trim_at_verbs(lowered) or lowered
            if trimmed in self.rules.slug_stopwords:
                continue
            return self._format_slug_candidate(trimmed)
        return None

    @staticmethod
    def _format_slug_candidate(value: str) -> str:
        if "." in value:
            parts = value.split(".")
            if len(parts) >= 2:
                first = parts[0].capitalize()
                rest = ".".join(part.lower() for part in parts[1:])
                return f"{first}.{rest}"
            return value
        tokens = [token for token in value.replace("-", " ").split() if token]
        if not tokens:
            return value
        return " ".join(token.capitalize() for token in tokens)

    def _normalize_candidates(
        self,
        raw_candidates: Sequence[tuple[str, str]],
    ) -> tuple[list[str], dict[str, str], dict[str, dict[str, Any]]]:
        normalized: list[str] = []
        methods: dict[str, str] = {}
        candidate_features: dict[str, dict[str, Any]] = {}
        seen: dict[str, int] = {}
        for value, method in raw_candidates:
            cleaned, normalization_meta = self._clean_candidate(value)
            if not cleaned:
                continue
            dedupe_key = cleaned.lower()
            priority = self.METHOD_PRIORITY.get(method, 0)
            existing_priority = seen.get(dedupe_key)
            if existing_priority is not None:
                if priority > existing_priority:
                    # Replace method while keeping original casing
                    for idx, candidate in enumerate(normalized):
                        if candidate.lower() == dedupe_key:
                            methods.pop(candidate, None)
                            candidate_features.pop(candidate, None)
                            normalized[idx] = cleaned
                            methods[cleaned] = method
                            candidate_features[cleaned] = normalization_meta or {}
                            break
                    seen[dedupe_key] = priority
                continue
            seen[dedupe_key] = priority
            normalized.append(cleaned)
            methods[cleaned] = method
            candidate_features[cleaned] = normalization_meta or {}
        return normalized, methods, candidate_features

    def _clean_candidate(self, value: str) -> tuple[str | None, dict[str, Any]]:
        text = unicodedata.normalize("NFKC", value or "")
        text = text.replace("’", "'").strip()
        text = re.sub(r"[\"“”]", "", text)
        text = re.sub(r"\s+", " ", text)
        text = text.strip(" -_,.")
        if not text:
            return None, {}

        repaired_possessive = False
        lower = text.lower()
        if lower.endswith("'s"):
            text = text[:-2]
            repaired_possessive = True
        elif lower.endswith("es") and len(text) > 4 and lower[-3] not in "aeiou":
            text = text[:-2]
            repaired_possessive = True
        elif lower.endswith("s") and not lower.endswith("ss") and len(text) > 3:
            text = text[:-1]
            repaired_possessive = True

        text = text.strip(" -_,.")
        if not text:
            return None, {}

        tokens = text.split()
        if len(tokens) > 6:
            tokens = tokens[:6]
            text = " ".join(tokens)

        letters = [ch for ch in text if ch.isalpha()]
        if letters and not any(ch in "aeiouyAEIOUY" for ch in letters):
            return None, {}

        meta = {"possessive_plural_repaired": repaired_possessive}
        return text or None, meta

    def _is_publisher_phrase(self, candidate: str, title: str, url: str) -> bool:
        lowered = (candidate or "").lower()
        text = f"{title.lower()} {lowered}".strip()
        if any(keyword in text for keyword in self.rules.publisher_keywords):
            return True
        domain = self._normalized_domain(url)
        return bool(domain and domain in self.rules.publisher_domains)

    def _delimiter_direction(self, url: str) -> str:
        domain = self._normalized_domain(url)
        return self.rules.publisher_delimiter_direction.get(
            domain,
            self.rules.publisher_delimiter_direction.get("default", "left"),
        )

    @staticmethod
    def _normalized_domain(url: str | None) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host

    @staticmethod
    def _trim_at_verbs(text: str) -> str | None:
        for verb in ("raises", "secures", "lands", "announces", "bags", "nabs", "hauls", "closes"):
            needle = f"-{verb}"
            if needle in text:
                return text.split(needle, 1)[0].strip("-")
        return None

    @staticmethod
    def _join_text(*parts: str) -> str:
        return " ".join(part for part in parts if part)

    @staticmethod
    def _build_regex(verbs: Sequence[str]) -> re.Pattern[str]:
        cleaned = [verb.strip().lower() for verb in verbs if verb and verb.strip()]
        token = "|".join(re.escape(verb) for verb in cleaned) or "raises"
        pattern = rf"(?P<company>[A-Z][A-Za-z0-9&'().\-]+(?: [A-Z][A-Za-z0-9&'().\-]+){{0,5}})\s+(?:{token})"
        return re.compile(pattern, re.IGNORECASE)


def generate_candidates(
    *,
    input_path: Path,
    output_path: Path,
    rules_path: Path,
) -> dict[str, Any]:
    records = load_records(input_path)
    rules = load_rules(rules_path)
    generator = CandidateGenerator(rules)
    stats = CandidateStats()
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for idx, record in enumerate(records, start=1):
        payload, error = generator.generate(record, row_index=idx)
        if payload is None:
            skipped.append(
                {
                    "row_index": idx,
                    "skip_reason": error or "UNKNOWN_ERROR",
                    "raw_title": (record.get("title") or record.get("company") or "")[:120],
                }
            )
            stats.record({"publisher_flagged": False, "publisher_split_used": False, "url_slug_used": False}, 0)
            logger.info("ROW_SKIPPED index=%s reason=%s", idx, error)
            continue
        rows.append(
            {
                **payload,
                "ruleset_version": rules.version,
                "ruleset_sha256": rules.sha256,
                "generator_version": GENERATOR_VERSION,
            }
        )
        stats.record(payload["features"], len(payload["candidates"]))

    summary_metrics = stats.metrics()
    accuracy_estimate = (
        summary_metrics["items_with_candidates"] / summary_metrics["items_total"]
        if summary_metrics["items_total"]
        else 0
    )
    logger.info(
        "Candidate generation complete items_total=%s items_with_candidates=%s metrics=%s ruleset=%s sha=%s",
        stats.items_total,
        stats.items_with_candidates,
        summary_metrics,
        rules.version,
        rules.sha256,
    )
    telemetry = get_telemetry()
    telemetry.emit(
        module="candidate_generator",
        event="summary",
        items_total=stats.items_total,
        items_with_candidates=stats.items_with_candidates,
        metrics=summary_metrics,
        accuracy_estimate=round(accuracy_estimate, 3),
        normalizer_ruleset_version=rules.version,
        normalizer_ruleset_sha256=rules.sha256,
    )
    payload = {
        "generator_version": GENERATOR_VERSION,
        "ruleset_version": rules.version,
        "ruleset_sha256": rules.sha256,
        "items_total": stats.items_total,
        "items_with_candidates": stats.items_with_candidates,
        "metrics": stats.metrics(),
        "data": rows,
        "skipped": skipped,
    }
    write_output(output_path, payload)
    return payload


def _seed_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    stage = _clean_string(record.get("funding_stage") or record.get("stage"))
    amount = record.get("funding_amount") or record.get("amount")
    currency = _clean_string(record.get("funding_currency") or record.get("currency")) or "USD"
    announced = _clean_string(record.get("funding_date") or record.get("announced_date"))
    return {
        "funding_stage": stage,
        "funding_amount": amount,
        "funding_currency": currency,
        "announced_date": announced,
    }


def _clean_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate company-name candidates for Exa seeds.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input Exa seed JSON/JSONL(.gz).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Destination for candidates JSON.")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES, help="Path to normalizer rules YAML.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv or [])
    try:
        generate_candidates(input_path=args.input, output_path=args.output, rules_path=args.rules)
    except FileNotFoundError as exc:
        logger.error("INPUT_MISSING path=%s", exc)
        return 1
    except json.JSONDecodeError as exc:
        logger.error("ROW_PARSE_ERROR: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected candidate generation failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
