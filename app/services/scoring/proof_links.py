"""Hydrates proof metadata for each scoring breakdown item."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.models.company import CompanyProfile
from app.models.signal_breakdown import SignalEvidence, SignalProof

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL = 300.0
_SENSITIVE_KEYS = ("key", "token", "signature")


class ProofLinkError(RuntimeError):
    """Raised when proof metadata cannot be hydrated for a signal."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    proof: SignalProof


class ProofLinkHydrator:
    """Caches and hydrates structured proof metadata per scoring slug."""

    def __init__(
        self,
        *,
        default_sources: Mapping[str, str] | None = None,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL,
    ) -> None:
        self._defaults = self._prepare_defaults(default_sources or {})
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._lock = Lock()

    @property
    def cache_stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._cache_hits, "misses": self._cache_misses}

    def hydrate(self, company: CompanyProfile, slug: str) -> SignalProof:
        """Return the primary proof for the requested scoring slug."""
        proofs = self.hydrate_many(company, slug, limit=1)
        return proofs[0]

    def hydrate_many(
        self,
        company: CompanyProfile,
        slug: str,
        *,
        limit: int | None = None,
    ) -> list[SignalProof]:
        """Return one or more proofs for the requested scoring slug."""
        normalized_slug = slug.lower()
        proofs: list[SignalProof] = []
        seen_hashes: set[str] = set()
        evidence_matches = self._match_evidence(company.signals, normalized_slug)
        try:
            if evidence_matches:
                proofs.extend(
                    self._hydrate_from_evidence(
                        company=company,
                        matches=evidence_matches,
                        limit=limit,
                        seen_hashes=seen_hashes,
                    )
                )

            remaining = self._remaining_capacity(limit, len(proofs))
            if not proofs and self._has_capacity(remaining):
                proofs.extend(
                    self._hydrate_from_fallback(
                        company=company,
                        slug=normalized_slug,
                        limit=remaining,
                        seen_hashes=seen_hashes,
                    )
                )
        except ProofLinkError:
            raise
        except Exception as exc:
            logger.exception(
                "proof_links.unexpected_error",
                extra={"company_id": str(company.company_id), "slug": slug},
            )
            raise ProofLinkError(
                f"Unable to hydrate proof metadata: {exc}",
                code="424_EVIDENCE_SOURCE_DOWN",
            ) from exc

        if not proofs:
            logger.error(
                "proof_links.missing",
                extra={"company_id": str(company.company_id), "slug": slug},
            )
            raise ProofLinkError(
                f"No proof metadata available for '{slug}'",
                code="404_PROOF_NOT_FOUND",
            )
        return proofs[:limit] if limit else proofs

    def _hydrate_from_evidence(
        self,
        *,
        company: CompanyProfile,
        matches: list[SignalEvidence],
        limit: int | None,
        seen_hashes: set[str],
    ) -> list[SignalProof]:
        if limit == 0:
            return []
        proofs: list[SignalProof] = []
        for evidence in matches:
            key = evidence.proof_hash or self._cache_key(str(evidence.source_url), evidence.timestamp)
            proof = self._cache_lookup(
                key,
                lambda evidence=evidence: self._from_evidence(
                    company=company,
                    evidence=evidence,
                ),
            )
            appended = self._register_proof(proof, seen_hashes, proofs)
            if not appended:
                continue
            if limit is not None and len(proofs) >= limit:
                break
        return proofs

    def _hydrate_from_fallback(
        self,
        *,
        company: CompanyProfile,
        slug: str,
        limit: int | None,
        seen_hashes: set[str],
    ) -> list[SignalProof]:
        if limit == 0:
            return []
        urls = self._fallback_urls(company, slug)
        proofs: list[SignalProof] = []
        if not urls:
            return proofs
        for url in urls:
            key = self._cache_key(url, None)
            proof = self._cache_lookup(
                key,
                lambda url=url: self._build_fallback_proof(
                    company=company,
                    slug=slug,
                    url=url,
                ),
            )
            appended = self._register_proof(proof, seen_hashes, proofs)
            if not appended:
                continue
            if limit is not None and len(proofs) >= limit:
                break
        return proofs

    def _from_evidence(self, *, company: CompanyProfile, evidence: SignalEvidence) -> SignalProof:
        sanitized_url = self._sanitize_url(str(evidence.source_url))
        proof = SignalProof(
            source_url=sanitized_url,
            verified_by=evidence.verified_by or company.verified_sources,
            timestamp=evidence.timestamp,
            proof_hash=evidence.proof_hash,
            source_hint=evidence.source_hint,
        )
        logger.info(
            "proof_links.hydrated",
            extra={
                "company_id": str(company.company_id),
                "slug": evidence.slug,
                "cache": "miss",
            },
        )
        return proof

    def _match_evidence(
        self,
        signals: Iterable[SignalEvidence],
        slug: str,
    ) -> list[SignalEvidence]:
        return [signal for signal in signals if signal.slug.lower() == slug]

    def _fallback_urls(self, company: CompanyProfile, slug: str) -> list[str]:
        if company.buying_signals:
            return self._unique_sanitized_urls(str(url) for url in company.buying_signals)
        candidate = self._defaults.get(slug)
        return [candidate] if candidate else []

    def _build_fallback_proof(self, *, company: CompanyProfile, slug: str, url: str) -> SignalProof:
        proof = SignalProof(
            source_url=url,
            verified_by=company.verified_sources,
            timestamp=None,
        )
        logger.info(
            "proof_links.fallback_used",
            extra={"company_id": str(company.company_id), "slug": slug},
        )
        return proof

    def _cache_lookup(self, key: str, factory: Callable[[], SignalProof]) -> SignalProof:
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry.expires_at > now:
                self._cache_hits += 1
                logger.debug("proof_links.cache_hit", extra={"key": key})
                return entry.proof
            self._cache_misses += 1
        proof = factory()
        with self._lock:
            self._cache[key] = _CacheEntry(expires_at=time.time() + self._cache_ttl, proof=proof)
        return proof

    @staticmethod
    def _register_proof(proof: SignalProof, seen_hashes: set[str], bucket: list[SignalProof]) -> bool:
        identifier = proof.proof_hash or str(proof.source_url)
        if identifier in seen_hashes:
            return False
        seen_hashes.add(identifier)
        bucket.append(proof)
        return True

    @staticmethod
    def _remaining_capacity(limit: int | None, collected: int) -> int | None:
        if limit is None:
            return None
        remaining = limit - collected
        return max(0, remaining)

    @staticmethod
    def _has_capacity(limit: int | None) -> bool:
        return limit is None or limit > 0

    @classmethod
    def _unique_sanitized_urls(cls, urls: Iterable[str]) -> list[str]:
        sanitized: list[str] = []
        seen: set[str] = set()
        for url in urls:
            clean = cls._sanitize_url(url)
            if clean in seen:
                continue
            seen.add(clean)
            sanitized.append(clean)
        return sanitized

    @staticmethod
    def _prepare_defaults(defaults: Mapping[str, str]) -> dict[str, str]:
        sanitized: dict[str, str] = {}
        for slug, url in defaults.items():
            if not slug or not url:
                continue
            lowered = slug.lower()
            sanitized[lowered] = ProofLinkHydrator._sanitize_url(url)
        return sanitized

    @staticmethod
    def _cache_key(source_url: str, timestamp: Any) -> str:
        value = f"{source_url}|{timestamp.isoformat() if timestamp else ''}"
        return value

    @staticmethod
    def _sanitize_url(url: str) -> str:
        parsed = urlparse(url)
        scheme = parsed.scheme or "https"
        if scheme == "http":
            scheme = "https"
        filtered = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not any(token in key.lower() for token in _SENSITIVE_KEYS)
        ]
        sanitized = parsed._replace(
            scheme=scheme,
            query=urlencode(filtered, doseq=True),
        )
        return urlunparse(sanitized)
