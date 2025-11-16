"""Hydrates proof metadata for each scoring breakdown item."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import settings
from app.models.company import CompanyProfile
from app.models.signal_breakdown import SignalEvidence, SignalProof, SignalProofValidationError
from app.observability.metrics import metrics

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
        attempts = 0
        error_code: str | None = None
        status = "success"

        def _tracked_lookup(
            key: str,
            factory: Callable[[], SignalProof],
            *,
            slug: str | None = None,
        ) -> SignalProof:
            nonlocal attempts
            attempts += 1
            return self._cache_lookup(key, factory, slug=slug)

        proof_count = 0
        start = time.perf_counter()
        try:
            if evidence_matches:
                proofs.extend(
                    self._hydrate_from_evidence(
                        company=company,
                        matches=evidence_matches,
                        limit=limit,
                        seen_hashes=seen_hashes,
                        cache_lookup=_tracked_lookup,
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
                        cache_lookup=_tracked_lookup,
                    )
                )
            if not proofs:
                logger.error(
                    "proof_links.missing",
                    extra={"company_id": str(company.company_id), "slug": slug},
                )
                raise ProofLinkError(
                    f"No proof metadata available for '{slug}'",
                    code="404_PROOF_NOT_FOUND",
                )
            result = proofs[:limit] if limit else proofs
            proof_count = len(result)
            return result
        except ProofLinkError as exc:
            status = "error"
            error_code = exc.code
            metrics.increment(
                "hydrator.errors",
                tags={"slug": normalized_slug, "code": exc.code, "mode": settings.fund_signal_mode},
            )
            raise
        except Exception as exc:
            status = "error"
            error_code = "424_EVIDENCE_SOURCE_DOWN"
            metrics.increment(
                "hydrator.errors",
                tags={"slug": normalized_slug, "code": error_code, "mode": settings.fund_signal_mode},
            )
            logger.exception(
                "proof_links.unexpected_error",
                extra={"company_id": str(company.company_id), "slug": slug},
            )
            raise ProofLinkError(
                f"Unable to hydrate proof metadata: {exc}",
                code="424_EVIDENCE_SOURCE_DOWN",
            ) from exc
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            metric_tags = {
                "slug": normalized_slug,
                "company_id": str(company.company_id),
                "status": status,
                "mode": settings.fund_signal_mode,
            }
            metrics.timing("hydrator.latency_ms", elapsed_ms, tags=metric_tags)
            metrics.gauge("hydrator.attempts", attempts, tags=metric_tags)
            metrics.gauge("hydrator.proof_count", proof_count, tags=metric_tags)
            self._log_outage_event(
                company_id=str(company.company_id),
                slug=normalized_slug,
                status=status,
                attempts=attempts,
                latency_ms=round(elapsed_ms, 4),
                proof_count=proof_count,
                error_code=error_code,
            )

    def _hydrate_from_evidence(
        self,
        *,
        company: CompanyProfile,
        matches: list[SignalEvidence],
        limit: int | None,
        seen_hashes: set[str],
        cache_lookup: Callable[..., SignalProof] | None = None,
    ) -> list[SignalProof]:
        if limit == 0:
            return []
        lookup = cache_lookup or self._cache_lookup
        proofs: list[SignalProof] = []
        for evidence in matches:
            key = evidence.proof_hash or self._cache_key(str(evidence.source_url), evidence.timestamp)
            proof = lookup(
                key,
                lambda evidence=evidence: self._from_evidence(
                    company=company,
                    evidence=evidence,
                ),
                slug=evidence.slug,
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
        cache_lookup: Callable[..., SignalProof] | None = None,
    ) -> list[SignalProof]:
        if limit == 0:
            return []
        urls = self._fallback_urls(company, slug)
        proofs: list[SignalProof] = []
        if not urls:
            return proofs
        lookup = cache_lookup or self._cache_lookup
        for url in urls:
            key = self._cache_key(url, None)
            proof = lookup(
                key,
                lambda url=url: self._build_fallback_proof(
                    company=company,
                    slug=slug,
                    url=url,
                ),
                slug=slug,
            )
            appended = self._register_proof(proof, seen_hashes, proofs)
            if not appended:
                continue
            if limit is not None and len(proofs) >= limit:
                break
        return proofs

    def _from_evidence(self, *, company: CompanyProfile, evidence: SignalEvidence) -> SignalProof:
        if evidence.timestamp is None:
            logger.error(
                "proof_links.missing_timestamp",
                extra={
                    "company_id": str(company.company_id),
                    "slug": evidence.slug,
                    "source_url": str(evidence.source_url),
                },
            )
            raise ProofLinkError(
                f"Proof timestamp missing for slug '{evidence.slug}'.",
                code="422_PROOF_MISSING_TIMESTAMP",
            )
        sanitized_url = self._sanitize_url(str(evidence.source_url))
        proof = SignalProof(
            source_url=sanitized_url,
            verified_by=evidence.verified_by or company.verified_sources,
            timestamp=evidence.timestamp,
            proof_hash=evidence.proof_hash,
            source_hint=evidence.source_hint,
        )
        return self._validate_proof(
            proof,
            company=company,
            slug=evidence.slug,
            source_url=sanitized_url,
        )

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
            timestamp=_utcnow(),
        )
        logger.info(
            "proof_links.fallback_used",
            extra={"company_id": str(company.company_id), "slug": slug},
        )
        return self._validate_proof(
            proof,
            company=company,
            slug=slug,
            source_url=url,
        )

    def _cache_lookup(
        self,
        key: str,
        factory: Callable[[], SignalProof],
        *,
        slug: str | None = None,
    ) -> SignalProof:
        now = time.time()
        tag_slug = slug or "unknown"
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry.expires_at > now:
                self._cache_hits += 1
                metrics.increment(
                    "hydrator.cache_hit",
                    tags={"slug": tag_slug, "mode": settings.fund_signal_mode},
                )
                logger.debug("proof_links.cache_hit", extra={"key": key})
                return entry.proof
            self._cache_misses += 1
        metrics.increment(
            "hydrator.cache_miss",
            tags={"slug": tag_slug, "mode": settings.fund_signal_mode},
        )
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

    def _log_outage_event(
        self,
        *,
        company_id: str,
        slug: str,
        status: str,
        attempts: int,
        latency_ms: float,
        proof_count: int,
        error_code: str | None,
    ) -> None:
        logger.info(
            "proof_hydrator.outage_sim",
            extra={
                "company_id": company_id,
                "slug": slug,
                "status": status,
                "attempts": attempts,
                "latency_ms": latency_ms,
                "proof_count": proof_count,
                "error_code": error_code,
            },
        )

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

    def _validate_proof(
        self,
        proof: SignalProof,
        *,
        company: CompanyProfile,
        slug: str,
        source_url: str,
    ) -> SignalProof:
        try:
            proof.ensure_fresh(settings.proof_max_age_days)
        except SignalProofValidationError as exc:
            log_payload = {
                "company_id": str(company.company_id),
                "slug": slug,
                "source_url": source_url,
                "timestamp": proof.timestamp.isoformat(),
                **exc.context,
            }
            event = "proof_links.stale_proof" if exc.code == "422_PROOF_STALE" else "proof_links.invalid_proof"
            logger.warning(event, extra=log_payload)
            raise ProofLinkError(str(exc), code=exc.code) from exc
        logger.info(
            "proof_links.hydrated",
            extra={
                "company_id": str(company.company_id),
                "slug": slug,
                "cache": "miss",
            },
        )
        return proof


def _utcnow() -> datetime:
    return datetime.now(UTC)


def sanitize_proof_url(url: str) -> str:
    """Public helper that sanitizes proof URLs for downstream consumers."""
    return ProofLinkHydrator._sanitize_url(url)
