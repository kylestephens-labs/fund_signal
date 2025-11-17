"""Domain models for company scoring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, computed_field, conint, model_validator

from app.models.signal_breakdown import SignalEvidence, SignalProof


class BreakdownItem(BaseModel):
    """Explainable component of an AI-derived score."""

    reason: str
    points: conint(ge=-100, le=100)  # type: ignore[valid-type]
    proof: SignalProof
    proofs: list[SignalProof] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_proof(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        proofs = values.get("proofs")
        if "proof" not in values and isinstance(proofs, Sequence) and proofs:
            first = proofs[0]
            if isinstance(first, SignalProof):
                values["proof"] = first
            elif isinstance(first, Mapping):
                values["proof"] = dict(first)
        if "proof" in values:
            return values
        source_url = values.get("source_url")
        if not source_url:
            raise ValueError("Breakdown proof must include a source_url.")
        values["proof"] = {
            "source_url": source_url,
            "verified_by": values.get("verified_by", []),
            "timestamp": values.get("timestamp"),
        }
        return values

    @model_validator(mode="after")
    def _ensure_proof_list(self) -> "BreakdownItem":
        normalized = list(self.proofs) if self.proofs else [self.proof]
        self.proofs = normalized
        self.proof = normalized[0]
        return self

    @computed_field  # type: ignore[misc]
    def source_url(self) -> HttpUrl:
        return self.proof.source_url

    @computed_field  # type: ignore[misc]
    def verified_by(self) -> list[str]:
        return self.proof.verified_by

    @computed_field  # type: ignore[misc]
    def timestamp(self) -> datetime | None:
        return self.proof.timestamp


class CompanyProfile(BaseModel):
    """Normalized input required for scoring."""

    company_id: UUID
    name: str
    funding_amount: str
    funding_stage: str
    days_since_funding: conint(ge=0)  # type: ignore[valid-type]
    employee_count: conint(ge=0)  # type: ignore[valid-type]
    job_postings: conint(ge=0)  # type: ignore[valid-type]
    tech_stack: list[str] = Field(default_factory=list)
    buying_signals: list[HttpUrl] = Field(default_factory=list)
    verified_sources: list[str] = Field(
        default_factory=list,
        description="Evidence providers confirming data quality.",
    )
    signals: list[SignalEvidence] = Field(
        default_factory=list,
        description="Optional structured proof metadata keyed by scoring slug.",
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CompanyScore(BaseModel):
    """Persisted result of the scoring engine."""

    id: UUID | None = None
    company_id: UUID
    score: conint(ge=0, le=100)  # type: ignore[valid-type]
    breakdown: list[BreakdownItem]
    recommended_approach: str
    pitch_angle: str
    scoring_model: str
    scoring_run_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _default_updated_at(self) -> "CompanyScore":
        if self.updated_at is None or self.updated_at < self.created_at:
            self.updated_at = self.created_at
        return self
