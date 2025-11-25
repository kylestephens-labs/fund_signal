"""Structured signal + proof metadata used by the scoring engine."""
# ruff: noqa: UP017

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator

_SENSITIVE_LABELS = {"api", "key", "token", "secret"}


def _normalized_labels(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        label = value.strip()
        if not label:
            continue
        normalized = label.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(label)
    return ordered


def _compute_hash(source_url: str, timestamp: datetime | None) -> str:
    payload = f"{source_url}|{timestamp.isoformat() if timestamp else ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SignalProofValidationError(ValueError):
    """Raised when proof metadata is invalid."""

    def __init__(self, message: str, code: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.context = context or {}


class SignalProof(BaseModel):
    """Proof metadata that backs a single scoring signal."""

    source_url: HttpUrl
    verified_by: list[str] = Field(default_factory=list)
    timestamp: datetime
    proof_hash: str | None = Field(
        default=None,
        description="Deterministic hash used for caching/deduplication.",
    )
    source_hint: str | None = Field(
        default=None,
        description="Human-friendly label for the proof source.",
    )

    @model_validator(mode="after")
    def _finalize(self) -> SignalProof:
        self.verified_by = _normalized_labels(self.verified_by)
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = self.timestamp.astimezone(timezone.utc)
        if not self.proof_hash:
            self.proof_hash = _compute_hash(str(self.source_url), self.timestamp)
        if any(token in (self.source_hint or "").lower() for token in _SENSITIVE_LABELS):
            self.source_hint = "redacted"
        return self

    def ensure_fresh(self, max_age_days: int) -> SignalProof:
        """Raise if proof is older than the configured freshness window."""
        if max_age_days <= 0:
            return self
        now = datetime.now(timezone.utc)
        age = now - self.timestamp
        if age > timedelta(days=max_age_days):
            raise SignalProofValidationError(
                f"Proof timestamp {self.timestamp.isoformat()} older than {max_age_days} days.",
                code="422_PROOF_STALE",
                context={"age_days": round(age.total_seconds() / 86400, 2)},
            )
        return self


class SignalEvidence(BaseModel):
    """Input evidence supplied by upstream providers (Exa, You.com, Tavily)."""

    slug: str = Field(
        ...,
        pattern=r"^[a-z0-9_\-]+$",
        description="Normalized signal bucket (funding/hiring/tech/team/signals).",
    )
    source_url: HttpUrl
    timestamp: datetime | None = None
    verified_by: list[str] = Field(default_factory=list)
    source_hint: str | None = None
    proof_hash: str | None = None

    def as_proof(self, *, fallback_verifiers: Iterable[str]) -> SignalProof:
        """Convert the evidence payload into cached SignalProof metadata."""
        verifiers = self.verified_by or list(fallback_verifiers)
        return SignalProof(
            source_url=self.source_url,
            verified_by=verifiers,
            timestamp=self.timestamp,
            proof_hash=self.proof_hash,
            source_hint=self.source_hint,
        )
