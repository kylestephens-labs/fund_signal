"""Shared helpers for normalizing cross-source article evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pipelines.io.schemas import FundingAmount, NormalizedSeed

# Common corporate suffixes to drop when comparing company names.
_CORPORATE_SUFFIXES = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "llc",
    "gmbh",
    "ag",
    "pty",
    "plc",
}

# Country-code public suffixes that require three labels to capture the registrable domain.
_TWO_PART_SUFFIXES = {
    "com.au",
    "net.au",
    "org.au",
    "gov.au",
    "edu.au",
    "com.br",
    "com.cn",
    "com.hk",
    "com.mx",
    "com.my",
    "com.sg",
    "com.tr",
    "com.tw",
    "com.sa",
    "co.in",
    "co.jp",
    "co.kr",
    "co.nz",
    "co.uk",
    "ac.uk",
    "gov.uk",
}

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_")
_SENSITIVE_TOKENS = ("key", "token", "signature")


@dataclass(frozen=True)
class ArticleEvidence:
    """Normalized article payload with match metadata."""

    source_id: str
    url: str
    canonical_url: str
    domain: str
    title: str
    published_at: str | None
    match_stage: bool
    match_amount: bool

    @property
    def confirms(self) -> bool:
        return self.match_stage or self.match_amount

    def to_confirmation(self) -> dict[str, object]:
        return {
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "published_at": self.published_at,
            "match": {"stage": self.match_stage, "amount": self.match_amount},
        }


class ArticleNormalizer:
    """Evaluate and normalize You.com/Tavily items against a seed."""

    def __init__(self, seed: NormalizedSeed) -> None:
        self._seed = seed
        self._company_aliases = _build_company_aliases(seed.company_name)
        self._stage_tokens = _build_stage_tokens(seed.funding_stage)
        self._amount_tokens = _build_amount_tokens(seed.amount)

    def normalize(
        self,
        *,
        source_id: str,
        title: str | None,
        snippet: str | None,
        url: str | None,
        published_at: str | None = None,
    ) -> ArticleEvidence | None:
        canonical_url = canonicalize_url(url)
        if not canonical_url:
            return None
        domain = normalize_domain(canonical_url)
        if not domain:
            return None

        title_value = (title or "").strip()
        haystack_raw = " ".join(part for part in (title_value, snippet) if part).strip()
        haystack_lower = haystack_raw.lower()
        normalized_text = _normalize_text(haystack_raw)
        compact_text = normalized_text.replace(" ", "")
        if not normalized_text:
            return None
        if not _matches_company(normalized_text, compact_text, self._company_aliases):
            return None

        stage_match = _matches_stage(normalized_text, haystack_lower, self._stage_tokens)
        amount_match = _matches_amount(haystack_lower, self._amount_tokens)

        return ArticleEvidence(
            source_id=source_id,
            url=canonical_url,
            canonical_url=canonical_url,
            domain=domain,
            title=title_value or canonical_url,
            published_at=normalize_timestamp(published_at),
            match_stage=stage_match,
            match_amount=amount_match,
        )


def slugify(value: str) -> str:
    """Create a deterministic slug for mapping bundle fixtures."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned


def canonicalize_url(url: str | None) -> str | None:
    """Normalize URLs for deduplication and downstream display."""
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    if not parsed.netloc:
        return None
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES)
        and not any(token in key.lower() for token in _SENSITIVE_TOKENS)
    ]
    sanitized = parsed._replace(query=urlencode(filtered_query, doseq=True), fragment="")
    return urlunparse(sanitized)


def normalize_domain(value: str | None) -> str:
    """Collapse hosts into a comparable domain token."""
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.netloc or parsed.path).lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in host.split(".") if part]
    if len(parts) <= 2:
        return host
    suffix = ".".join(parts[-2:])
    if suffix in _TWO_PART_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    normalized = candidate.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _matches_company(normalized_text: str, compact_text: str, aliases: set[str]) -> bool:
    for alias in aliases:
        if not alias:
            continue
        if " " in alias:
            if alias in normalized_text:
                return True
        else:
            if alias in compact_text:
                return True
    return False


def _matches_stage(normalized_text: str, haystack_lower: str, tokens: set[str]) -> bool:
    for token in tokens:
        if not token:
            continue
        if " " in token or "-" in token:
            if token in normalized_text:
                return True
        else:
            if token in haystack_lower:
                return True
    return False


def _matches_amount(haystack_lower: str, tokens: set[str]) -> bool:
    return any(token and token in haystack_lower for token in tokens)


def _build_company_aliases(company: str) -> set[str]:
    normalized = _normalize_text(company)
    aliases = {normalized, normalized.replace(" ", ""), slugify(company).replace("-", "")}
    words = normalized.split()
    if words and words[-1] in _CORPORATE_SUFFIXES:
        trimmed = " ".join(words[:-1]).strip()
        if trimmed:
            aliases.add(trimmed)
            aliases.add(trimmed.replace(" ", ""))
    return {alias for alias in aliases if alias}


def _build_stage_tokens(stage: str) -> set[str]:
    stage_lower = stage.strip().lower()
    tokens = {
        stage_lower,
        stage_lower.replace("-", " "),
        stage_lower.replace(" ", ""),
    }
    if stage_lower.startswith("series "):
        suffix = stage_lower.replace("series ", "").strip()
        tokens.add(f"series{suffix}")
        tokens.add(f"series-{suffix}")
    return {token for token in tokens if token}


def _build_amount_tokens(amount: FundingAmount | None) -> set[str]:
    if amount is None:
        return set()
    scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[amount.unit]
    absolute_value = int(round(amount.value * scale))
    display_value = f"{amount.value:g}"
    suffix_letter = {"K": "k", "M": "m", "B": "b"}[amount.unit]
    suffix_word = {"K": "thousand", "M": "million", "B": "billion"}[amount.unit]
    tokens = {
        f"{absolute_value:,}".lower(),
        f"${absolute_value:,}".lower(),
        f"{display_value}{suffix_letter}",
        f"${display_value}{suffix_letter}",
        f"{display_value} {suffix_word}",
        f"${display_value} {suffix_word}",
        f"{int(round(amount.value))}{suffix_letter}",
        f"${int(round(amount.value))}{suffix_letter}",
        f"{int(round(amount.value))} {suffix_word}",
        f"${int(round(amount.value))} {suffix_word}",
    }
    tokens.add(f"{display_value}{suffix_letter}".replace(" ", ""))
    return {token.lower() for token in tokens if token}
