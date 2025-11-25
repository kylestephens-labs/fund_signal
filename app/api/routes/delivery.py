"""Lead retrieval, delivery triggers, and billing endpoints."""
# ruff: noqa: UP017

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone  # noqa: UP017 - timezone.utc for py3.9 compatibility
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


_fixture_path = Path("tests/fixtures/scoring/regression_companies.json")
_cached_leads: list[dict[str, Any]] = []
_webhook_events: set[str] = set()
_subscriptions: dict[str, dict[str, Any]] = {}


def _load_fixture() -> list[dict[str, Any]]:
    if _cached_leads:
        return _cached_leads
    if not _fixture_path.exists():
        return []
    payload = json.loads(_fixture_path.read_text(encoding="utf-8"))
    companies = payload.get("companies", [])
    leads: list[dict[str, Any]] = []
    for entry in companies:
        profile = entry.get("profile") or {}
        leads.append(
            {
                "company_id": profile.get("company_id"),
                "score": entry.get("max_score", 0),
                "recommended_approach": "Personalized outreach via founder",
                "pitch_angle": "We accelerate outbound for funded SaaS teams.",
                "proofs": profile.get("buying_signals", []),
                "verified_sources": profile.get("verified_sources", []),
            }
        )
    _cached_leads.extend(leads)
    return leads


class LeadResponse(BaseModel):
    company_id: UUID
    score: int
    recommended_approach: str
    pitch_angle: str
    proofs: list[str] = Field(default_factory=list)
    verified_sources: list[str] = Field(default_factory=list)


@router.get("/leads", response_model=list[LeadResponse])
async def list_leads(score_gte: int = 0, limit: int = 25) -> list[LeadResponse]:
    """Return leads from the regression fixture; filters by minimum score."""
    leads = _load_fixture()
    filtered = [lead for lead in leads if int(lead.get("score", 0)) >= score_gte]
    sliced = filtered[: max(limit, 1)]
    logger.info(
        "leads.list",
        extra={"count": len(sliced), "score_gte": score_gte, "limit": limit},
    )
    return [LeadResponse(**lead) for lead in sliced]


class DeliveryTriggerRequest(BaseModel):
    force: bool | None = False


class DeliveryTriggerResponse(BaseModel):
    queued: bool
    output_paths: list[str]


def _write_placeholder_artifacts() -> list[str]:
    output_dir = Path(settings.delivery_output_dir or "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "email_delivery_stub.md"
    slack_path = output_dir / "slack_delivery_stub.json"
    if not markdown_path.exists():
        markdown_path.write_text("# Delivery stub\n", encoding="utf-8")
    if not slack_path.exists():
        slack_path.write_text(json.dumps({"message": "stub"}), encoding="utf-8")
    return [str(markdown_path), str(slack_path)]


@router.post("/delivery/weekly", response_model=DeliveryTriggerResponse, status_code=202)
async def trigger_delivery(_: DeliveryTriggerRequest) -> DeliveryTriggerResponse:
    """Queue weekly delivery; writes placeholder artifacts for offline/local runs."""
    paths = _write_placeholder_artifacts()
    logger.info("delivery.weekly.queued", extra={"artifacts": paths})
    return DeliveryTriggerResponse(queued=True, output_paths=paths)


class ReminderRequest(BaseModel):
    email: str
    channel: str = Field(pattern="^(email|slack)$")


@router.post("/delivery/reminder", response_model=DeliveryTriggerResponse, status_code=202)
async def send_reminder(payload: ReminderRequest) -> DeliveryTriggerResponse:
    """Queue a reminder notification."""
    paths = _write_placeholder_artifacts()
    logger.info("delivery.reminder", extra={"email": payload.email, "channel": payload.channel})
    return DeliveryTriggerResponse(queued=True, output_paths=paths)


class StripeWebhookPayload(BaseModel):
    id: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class WebhookResponse(BaseModel):
    received: bool
    duplicate: bool = False


@router.post("/billing/stripe/webhook", response_model=WebhookResponse)
async def stripe_webhook(payload: StripeWebhookPayload) -> WebhookResponse:
    """Accept Stripe webhook events with simple idempotency."""
    if payload.id in _webhook_events:
        logger.info("stripe.webhook.duplicate", extra={"event_id": payload.id})
        return WebhookResponse(received=True, duplicate=True)
    _webhook_events.add(payload.id)
    logger.info("stripe.webhook.received", extra={"event_id": payload.id, "type": payload.type})
    return WebhookResponse(received=True, duplicate=False)


class CancelRequest(BaseModel):
    email: str
    reason: str | None = None


class CancelResponse(BaseModel):
    status: str
    effective_date: str
    note: str | None = None


@router.post("/billing/cancel", response_model=CancelResponse)
async def cancel_subscription(payload: CancelRequest) -> CancelResponse:
    """Mark a subscription as cancelled for the provided email."""
    _write_placeholder_artifacts()
    effective_date = datetime.now(timezone.utc).isoformat()
    _subscriptions[payload.email] = {
        "status": "cancelled",
        "reason": payload.reason or "unspecified",
    }
    logger.info("billing.cancelled", extra={"email": payload.email, "reason": payload.reason})
    return CancelResponse(status="cancelled", effective_date=effective_date, note=payload.reason)
