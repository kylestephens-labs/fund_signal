"""Manifest metadata helpers (freshness watermark, expiry warnings)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final


@dataclass(frozen=True)
class FreshnessMetadata:
    bundle_id: str
    captured_at: datetime
    expiry_days: int
    expires_in_days: float
    watermark: str
    age_days: float
    warning: bool


WARNING_THRESHOLD: Final[float] = 0.75


def build_freshness_metadata(
    bundle_id: str,
    captured_at: datetime,
    expiry_days: int,
    *,
    now: datetime | None = None,
    warning_threshold: float = WARNING_THRESHOLD,
) -> FreshnessMetadata:
    """Derive freshness metadata used for logging and lead annotations."""
    now = _ensure_timezone(now or datetime.now(timezone.utc))
    captured_at = _ensure_timezone(captured_at)
    age = now - captured_at
    age_days = age.total_seconds() / 86400
    expires_in_days = max(0.0, expiry_days - age_days)
    watermark = _format_watermark(captured_at, expires_in_days)
    warning = bool(expiry_days) and age_days >= expiry_days * warning_threshold
    return FreshnessMetadata(
        bundle_id=bundle_id,
        captured_at=captured_at,
        expiry_days=expiry_days,
        expires_in_days=expires_in_days,
        watermark=watermark,
        age_days=age_days,
        warning=warning,
    )


def _format_watermark(captured_at: datetime, expires_in_days: float) -> str:
    expires_in_label = int(expires_in_days)
    return (
        f"Verified on {captured_at.strftime('%b %d, %Y')} • Expires in {expires_in_label} days"
    )


def _ensure_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
