"""Utility helpers for capture/normalization workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.models.lead import CompanyFunding

SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify_company(name: str) -> str:
    """Create a filesystem-friendly slug for a company name."""
    slug = SLUG_PATTERN.sub("-", name.lower()).strip("-")
    return slug or "company"


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def serialize_lead(lead: CompanyFunding) -> dict[str, Any]:
    """Create a JSON-serializable representation of a CompanyFunding instance."""
    return lead.model_dump(mode="json")
