"""SQLModel mapping for stored scoring records."""
# ruff: noqa: UP017

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Column, DateTime, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression
from sqlmodel import Field, SQLModel

from app.models.company import BreakdownItem, CompanyScore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


JSON_BACKING_TYPE = sa.JSON().with_variant(JSONB(astext_type=sa.Text()), "postgresql")


class UtcNow(expression.FunctionElement):
    """Dialect-aware server default that pins timestamps to UTC."""

    type = DateTime(timezone=True)
    inherit_cache = True


@compiles(UtcNow)
def _utc_now_default(
    element, compiler, **kwargs
) -> str:  # pragma: no cover - trivial sql generator
    return "CURRENT_TIMESTAMP"


@compiles(UtcNow, "postgresql")
def _utc_now_default_postgres(
    element, compiler, **kwargs
) -> str:  # pragma: no cover - trivial sql generator
    return "timezone('utc', now())"


class ScoreRecord(SQLModel, table=True):
    """ORM model for persisted CompanyScore rows."""

    __tablename__ = "scores"
    __table_args__ = (
        sa.UniqueConstraint("company_id", "scoring_run_id", name="uq_scores_company_run"),
        sa.Index("ix_scores_company_id", "company_id"),
        sa.Index("ix_scores_scoring_run", "scoring_run_id"),
    )

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(Uuid(as_uuid=True), primary_key=True, nullable=False),
    )
    company_id: UUID = Field(
        sa_column=Column(Uuid(as_uuid=True), nullable=False),
    )
    scoring_run_id: str = Field(
        sa_column=Column(String(length=255), nullable=False),
    )
    score: int = Field(sa_column=Column(Integer, nullable=False))
    breakdown: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON_BACKING_TYPE, nullable=False),
    )
    recommended_approach: str = Field(
        sa_column=Column(String(length=512), nullable=False),
    )
    pitch_angle: str = Field(
        sa_column=Column(String(length=512), nullable=False),
    )
    scoring_model: str = Field(
        sa_column=Column(String(length=255), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=UtcNow(),
        ),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=UtcNow(),
            onupdate=UtcNow(),
        ),
    )

    @classmethod
    def from_company_score(cls, score: CompanyScore) -> ScoreRecord:
        """Convert an in-memory CompanyScore into a persistence row."""
        persisted_id = score.id or uuid4()
        breakdown_payload = [item.model_dump(mode="json") for item in score.breakdown]
        return cls(
            id=persisted_id,
            company_id=score.company_id,
            scoring_run_id=score.scoring_run_id,
            score=score.score,
            breakdown=breakdown_payload,
            recommended_approach=score.recommended_approach,
            pitch_angle=score.pitch_angle,
            scoring_model=score.scoring_model,
            created_at=score.created_at,
            updated_at=score.updated_at or score.created_at,
        )

    def to_company_score(self) -> CompanyScore:
        """Hydrate a CompanyScore domain model from the stored JSON payload."""
        breakdown_items = [BreakdownItem(**entry) for entry in self.breakdown]
        return CompanyScore(
            id=self.id,
            company_id=self.company_id,
            score=self.score,
            breakdown=breakdown_items,
            recommended_approach=self.recommended_approach,
            pitch_angle=self.pitch_angle,
            scoring_model=self.scoring_model,
            scoring_run_id=self.scoring_run_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
