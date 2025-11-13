"""Domain models for company scoring."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, conint


class BreakdownItem(BaseModel):
    """Explainable component of an AI-derived score."""

    reason: str
    points: conint(ge=-100, le=100)  # type: ignore[valid-type]
    source_url: HttpUrl
    verified_by: list[str] = Field(default_factory=list)
    timestamp: datetime | None = None


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


class CompanyScore(BaseModel):
    """Persisted result of the scoring engine."""

    company_id: UUID
    score: conint(ge=0, le=100)  # type: ignore[valid-type]
    breakdown: list[BreakdownItem]
    recommended_approach: str
    pitch_angle: str
    scoring_model: str
    scoring_run_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
