"""Shared data schemas used across pipeline tools."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl
from pydantic.config import ConfigDict


class FundingAmount(BaseModel):
    """Normalized representation of a funding amount."""

    value: float = Field(..., ge=0)
    unit: Literal["K", "M", "B"]
    currency: Literal["USD", "EUR", "GBP"] = "USD"

    model_config = ConfigDict(frozen=True, extra="forbid")


class NormalizedSeed(BaseModel):
    """Normalized Exa seed ready for downstream verification."""

    company_name: str = Field(..., min_length=1)
    funding_stage: str = Field(..., min_length=1)
    amount: FundingAmount
    announced_date: date | None = None
    source_url: HttpUrl
    raw_title: str | None = None
    raw_snippet: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")
