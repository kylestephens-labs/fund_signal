"""Lead retrieval, delivery triggers, and billing endpoints."""
# ruff: noqa: UP017

from __future__ import annotations

import hmac
import json
import logging
from datetime import (  # noqa: UP017 - timezone.utc for py3.9 compatibility
    datetime,
    timedelta,
    timezone,
)
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.api.routes.auth import SessionContext, require_session
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


_fixture_path = Path("tests/fixtures/scoring/regression_companies.json")
_cached_leads: list[dict[str, Any]] = []
_fixture_generated_at: str | None = None
_webhook_events: set[str] = set()
_subscriptions: dict[str, dict[str, Any]] = {}

TRIAL_DAYS = 14


def _load_fixture() -> list[dict[str, Any]]:
    if _cached_leads:
        return _cached_leads
    if not _fixture_path.exists():
        return []
    payload = json.loads(_fixture_path.read_text(encoding="utf-8"))
    companies = payload.get("companies", [])
    leads: list[dict[str, Any]] = []
    global _fixture_generated_at
    if not _fixture_generated_at:
        _fixture_generated_at = datetime.now(timezone.utc).isoformat()
    generated_at = _fixture_generated_at
    for entry in companies:
        profile = entry.get("profile") or {}
        proofs = profile.get("buying_signals", [])
        leads.append(
            {
                "company_id": profile.get("company_id"),
                "score": entry.get("max_score", 0),
                "recommended_approach": "Personalized outreach via founder",
                "pitch_angle": "We accelerate outbound for funded SaaS teams.",
                "proofs": proofs,
                "verified_sources": profile.get("verified_sources", []),
                "freshness": generated_at,
                "report_generated_at": generated_at,
                "proof_count": len(proofs),
                "upgrade_cta": "Upgrade to keep weekly deliveries coming.",
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
    freshness: str
    report_generated_at: str
    proof_count: int
    upgrade_cta: str


class SubscribeRequest(BaseModel):
    price_id: str | None = None
    plan_id: str | None = None
    payment_method_id: str
    customer_email: EmailStr

    def resolved_plan(self) -> str:
        plan = self.plan_id or self.price_id
        if not plan:
            raise HTTPException(status_code=400, detail="plan_id or price_id is required")
        return plan


class SubscribeResponse(BaseModel):
    subscription_id: str
    status: str
    trial_start: str
    trial_end: str
    current_period_end: str
    plan_id: str


class CancelRequest(BaseModel):
    subscription_id: str | None = None
    email: EmailStr | None = None
    reason: str | None = None


class CancelResponse(BaseModel):
    status: str
    effective_date: str
    note: str | None = None


@router.get("/leads", response_model=list[LeadResponse])
async def list_leads(
    score_gte: int = 0,
    limit: int = 25,
    session: SessionContext = Depends(require_session),
) -> list[LeadResponse]:
    """Return leads from the regression fixture; filters by minimum score."""
    leads = _load_fixture()
    filtered = [lead for lead in leads if int(lead.get("score", 0)) >= score_gte]
    bounded_limit = min(max(limit, 1), 50)
    sliced = filtered[:bounded_limit]
    logger.info(
        "leads.list",
        extra={
            "count": len(sliced),
            "score_gte": score_gte,
            "limit": bounded_limit,
            "email_domain": session.email.split("@")[-1] if "@" in session.email else "*",
        },
    )
    return [LeadResponse(**lead) for lead in sliced]


def _trial_window() -> tuple[str, str]:
    trial_start_dt = datetime.now(timezone.utc)
    trial_end_dt = trial_start_dt + timedelta(days=TRIAL_DAYS)
    return trial_start_dt.isoformat(), trial_end_dt.isoformat()


def _mask_email(email: str | None) -> str:
    if not email:
        return "*"
    domain = email.split("@")[-1] if "@" in email else ""
    return f"*@{domain}" if domain else "*"


@router.post("/billing/subscribe", response_model=SubscribeResponse)
async def subscribe(
    payload: SubscribeRequest, session: SessionContext = Depends(require_session)
) -> SubscribeResponse:
    """Create a subscription with a 14-day trial; no immediate charge."""
    plan_id = payload.resolved_plan()
    subscription_id = f"sub_{uuid4().hex}"
    trial_start, trial_end = _trial_window()
    record = {
        "subscription_id": subscription_id,
        "status": "trialing",
        "trial_start": trial_start,
        "trial_end": trial_end,
        "current_period_end": trial_end,
        "plan_id": plan_id,
        "cancel_at_period_end": False,
        "email": payload.customer_email,
        "payment_method_id": payload.payment_method_id,
    }
    _subscriptions[subscription_id] = record
    logger.info(
        "stripe.subscribe.created",
        extra={
            "subscription_id": subscription_id,
            "plan_id": plan_id,
            "trial_end": trial_end,
            "email_domain": _mask_email(payload.customer_email),
        },
    )
    return SubscribeResponse(
        subscription_id=subscription_id,
        status=record["status"],
        trial_start=trial_start,
        trial_end=trial_end,
        current_period_end=record["current_period_end"],
        plan_id=plan_id,
    )


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


def _verify_signature(payload: bytes, signature_header: str | None) -> None:
    """Lightweight signature verification compatible with Stripe's v1 scheme."""
    if not settings.stripe_webhook_secret:
        return
    if not signature_header:
        logger.warning("stripe.webhook.signature_missing")
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        pairs = dict(entry.split("=", 1) for entry in signature_header.split(","))
        timestamp = pairs.get("t")
        signature = pairs.get("v1")
    except Exception:  # pragma: no cover - defensive
        logger.warning("stripe.webhook.signature_parse_failed")
        raise HTTPException(status_code=403, detail="Invalid signature")
    if not timestamp or not signature:
        logger.warning("stripe.webhook.signature_parts_missing")
        raise HTTPException(status_code=403, detail="Invalid signature")
    expected = hmac.new(
        settings.stripe_webhook_secret.encode(),
        msg=f"{timestamp}.{payload.decode()}".encode(),
        digestmod=sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        logger.warning("stripe.webhook.signature_mismatch")
        raise HTTPException(status_code=403, detail="Invalid signature")


def _apply_subscription_event(payload: StripeWebhookPayload) -> None:
    obj = payload.data.get("object") if payload.data else {}
    sub_id = obj.get("id")
    if not sub_id:
        return
    record = _subscriptions.setdefault(
        sub_id,
        {
            "subscription_id": sub_id,
            "plan_id": obj.get("plan", {}).get("id") if obj.get("plan") else None,
            "email": obj.get("customer_email"),
        },
    )
    status = obj.get("status")
    if status:
        record["status"] = status
    record["trial_start"] = obj.get("trial_start", record.get("trial_start"))
    record["trial_end"] = obj.get("trial_end", record.get("trial_end"))
    record["current_period_end"] = obj.get("current_period_end", record.get("current_period_end"))
    record["cancel_at_period_end"] = obj.get(
        "cancel_at_period_end", record.get("cancel_at_period_end", False)
    )
    _subscriptions[sub_id] = record


def _resolve_subscription(payload: CancelRequest) -> dict[str, Any]:
    sub_id = payload.subscription_id
    if sub_id:
        subscription = _subscriptions.get(sub_id)
        if not subscription:
            logger.warning("stripe.cancel.not_found", extra={"subscription_id": sub_id})
            raise HTTPException(status_code=404, detail="Subscription not found")
        return subscription
    if payload.email:
        for record in _subscriptions.values():
            if record.get("email") == payload.email:
                return record
    logger.warning("stripe.cancel.no_target")
    raise HTTPException(status_code=404, detail="Subscription not found")


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
async def stripe_webhook(
    request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")
) -> WebhookResponse:
    """Accept Stripe webhook events with idempotency and optional signature verification."""
    body = await request.body()
    _verify_signature(body, stripe_signature)
    try:
        payload = StripeWebhookPayload.model_validate_json(body)
    except Exception:
        logger.warning("stripe.webhook.invalid_payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    if payload.id in _webhook_events:
        logger.info("stripe.webhook.duplicate", extra={"event_id": payload.id})
        return WebhookResponse(received=True, duplicate=True)
    _webhook_events.add(payload.id)
    _apply_subscription_event(payload)
    logger.info("stripe.webhook.received", extra={"event_id": payload.id, "type": payload.type})
    return WebhookResponse(received=True, duplicate=False)


@router.post("/billing/cancel", response_model=CancelResponse)
async def cancel_subscription(
    payload: CancelRequest, session: SessionContext = Depends(require_session)
) -> CancelResponse:
    """Mark a subscription as cancelled; idempotent and cancel-at-period-end aware."""
    subscription = _resolve_subscription(payload)
    subscription["cancel_at_period_end"] = True
    subscription["status"] = "canceled"
    subscription["reason"] = payload.reason or "unspecified"
    effective_date = subscription.get("current_period_end") or subscription.get("trial_end")
    if not effective_date:
        effective_date = datetime.now(timezone.utc).isoformat()
    logger.info(
        "billing.cancelled",
        extra={
            "subscription_id": subscription["subscription_id"],
            "email_domain": _mask_email(subscription.get("email")),
            "reason": payload.reason,
        },
    )
    return CancelResponse(status="cancelled", effective_date=effective_date, note=payload.reason)
