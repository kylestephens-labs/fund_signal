"""Domain models for lead discovery."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, HttpUrl


class CompanyFunding(BaseModel):
    """Normalized representation of a funding announcement."""

    company: str
    funding_amount: int
    funding_stage: str
    funding_date: date
    source_url: HttpUrl
    exa_found: bool = Field(default=True, description="True when sourced from Exa discovery.")
    discovered_at: datetime
    youcom_verified: bool = Field(default=False, description="True when verified via You.com.")
    youcom_verified_at: datetime | None = None
    news_sources: list[str] = Field(default_factory=list)
    press_articles: list[str] = Field(default_factory=list)
    tavily_verified: bool = Field(default=False, description="True when verified via Tavily.")
    tavily_verified_at: datetime | None = None
    tavily_reason: str | None = Field(default=None, description="Explanation when Tavily verification fails.")
    proof_links: list[str] = Field(default_factory=list)
    confidence: str | None = Field(default=None, description="Confidence label (VERIFIED/LIKELY/EXCLUDE).")
    verified_by: list[str] = Field(default_factory=list)
    last_checked_at: datetime | None = None
    freshness_watermark: str | None = None
    ingest_version: int = Field(default=1, description="Pipeline schema version.")

    model_config = {"from_attributes": True}
