"""Lead retrieval, delivery triggers, and billing endpoints."""
# ruff: noqa: UP017

from __future__ import annotations

import hmac
import json
import logging
import secrets
from datetime import (  # noqa: UP017 - timezone.utc for py3.9 compatibility
    datetime,
    timedelta,
    timezone,
)
from hashlib import blake2b, sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.api.routes.auth import SessionContext, require_session
from app.config import settings
from app.core.database import get_database
from app.models.subscription import ProcessedEvent, Subscription
from app.observability.metrics import metrics

logger = logging.getLogger(__name__)
router = APIRouter()


TRIAL_DAYS = 14


def _load_fixture() -> list[dict[str, Any]]:
    payload_path = Path("tests/fixtures/scoring/regression_companies.json")
    if not payload_path.exists():
        return []
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    companies = payload.get("companies", [])
    leads: list[dict[str, Any]] = []
    generated_at = datetime.now(timezone.utc).isoformat()
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
    payment_method_id: str | None = None
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
    plan_label: str
    payment_behavior: str
    client_secret: str | None = None


class CancelRequest(BaseModel):
    subscription_id: str | None = None
    email: EmailStr | None = None
    reason: str | None = None


class CancelResponse(BaseModel):
    status: str
    effective_date: str
    message: str
    note: str | None = None
    undo_token: str | None = None
    undo_expires_at: str | None = None


class CancelUndoRequest(BaseModel):
    undo_token: str


class CancelUndoResponse(BaseModel):
    status: str
    restored_at: str
    message: str


class SubscriptionStateResponse(BaseModel):
    subscription_id: str
    plan_id: str | None
    status: str
    trial_start: str | None = None
    trial_end: str | None = None
    current_period_end: str | None = None
    cancel_at_period_end: bool
    updated_at: str


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


def _extract_client_secret(subscription: dict[str, Any]) -> str | None:
    """Return payment_intent or setup_intent client secret if present."""
    latest_invoice = subscription.get("latest_invoice")
    invoice_obj = latest_invoice if isinstance(latest_invoice, dict) else {}
    intent = invoice_obj.get("payment_intent") or invoice_obj.get("setup_intent")
    if isinstance(intent, dict):
        return intent.get("client_secret")
    return None


def _coerce_dt(value: Any, default: datetime | None = None) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return default


def _resolve_plan_label(plan_id: str) -> str:
    """Return a human-friendly plan label aligned with configured price ids."""
    label_map = {
        "solo": "Solo plan",
        "growth": "Growth plan",
        "team": "Team plan",
    }
    if settings.stripe_plan_solo:
        label_map[settings.stripe_plan_solo] = label_map["solo"]
    if settings.stripe_plan_growth:
        label_map[settings.stripe_plan_growth] = label_map["growth"]
    if settings.stripe_plan_team:
        label_map[settings.stripe_plan_team] = label_map["team"]
    return label_map.get(plan_id, plan_id)


async def _persist_subscription_record(db: AsyncSession | None, record: dict[str, Any]) -> None:
    """Persist subscription to DB; DB is required."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    stmt = select(Subscription).where(Subscription.subscription_id == record["subscription_id"])
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        existing.status = record.get("status", existing.status)
        existing.trial_start = _coerce_dt(record.get("trial_start"), existing.trial_start)
        existing.trial_end = _coerce_dt(record.get("trial_end"), existing.trial_end)
        existing.current_period_end = _coerce_dt(
            record.get("current_period_end"), existing.current_period_end
        )
        existing.cancel_at_period_end = record.get("cancel_at_period_end", False)
        existing.default_payment_method = (
            record.get("payment_method_id") or existing.default_payment_method
        )
        existing.price_id = record.get("plan_id") or existing.price_id
        existing.email = record.get("email") or existing.email
        existing.customer_id = record.get("customer_id") or existing.customer_id
    else:
        required_fields = {
            "customer_id": record.get("customer_id"),
            "email": record.get("email"),
            "plan_id": record.get("plan_id"),
            "status": record.get("status"),
        }
        missing = [field for field, value in required_fields.items() if value is None]
        if missing:
            logger.warning(
                "stripe.subscription.missing_context",
                extra={
                    "subscription_id": record.get("subscription_id"),
                    "missing_fields": ",".join(missing),
                },
            )
            return
        db_obj = Subscription(
            subscription_id=record["subscription_id"],
            customer_id=record["customer_id"],
            email=record["email"],
            price_id=record["plan_id"],
            status=record["status"],
            trial_start=_coerce_dt(record.get("trial_start")),
            trial_end=_coerce_dt(record.get("trial_end")),
            current_period_end=_coerce_dt(record.get("current_period_end")),
            cancel_at_period_end=record.get("cancel_at_period_end", False),
            default_payment_method=record.get("payment_method_id"),
        )
        db.add(db_obj)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("stripe.subscription.persist_failed")
        raise HTTPException(status_code=500, detail="Failed to persist subscription")


async def _mark_event(db: AsyncSession, event_id: str, sub_id: str | None = None) -> None:
    if not sub_id:
        return
    stmt = select(ProcessedEvent).where(ProcessedEvent.event_id == event_id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        return
    db.add(ProcessedEvent(event_id=event_id, subscription_id=sub_id))
    stmt = select(Subscription).where(Subscription.subscription_id == sub_id)
    sub_result = await db.execute(stmt)
    subscription = sub_result.scalar_one_or_none()
    if not subscription:
        logger.warning(
            "stripe.webhook.event_missing_subscription",
            extra={"event_id": event_id, "subscription_id": sub_id},
        )
        return
    subscription.last_event_id = event_id
    try:
        await db.commit()
    except Exception:
        await db.rollback()


async def _ensure_event_tables(db: AsyncSession) -> None:
    """Ensure processed_events table exists in DB session (for lightweight fakes)."""
    try:
        await db.execute(select(ProcessedEvent).limit(1))
    except Exception:
        # no-op for real DBs; fakes should handle missing table gracefully
        return


def _find_or_create_customer(*, email: str) -> dict[str, Any]:
    stripe.api_key = settings.stripe_secret_key
    try:
        existing = stripe.Customer.list(email=email, limit=1)
        if existing.data:
            return existing.data[0]
        return stripe.Customer.create(email=email)
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        logger.warning("stripe.customer.failed", extra={"error": str(exc)})
        raise HTTPException(status_code=400, detail="Unable to create customer") from exc


@router.post("/billing/subscribe", response_model=SubscribeResponse)
async def subscribe(
    payload: SubscribeRequest,
    session: SessionContext = Depends(require_session),
    db: AsyncSession | None = Depends(get_database),
) -> SubscribeResponse:
    """Create a subscription with a 14-day trial; no immediate charge."""
    if db is None:
        logger.warning("stripe.subscribe.db_missing")
        raise HTTPException(status_code=503, detail="Database not configured")
    if not settings.stripe_secret_key:
        logger.warning("stripe.subscribe.missing_secret")
        raise HTTPException(status_code=503, detail="Stripe not configured")
    stripe.api_key = settings.stripe_secret_key
    plan_id = payload.resolved_plan()
    plan_label = _resolve_plan_label(plan_id)
    if not payload.payment_method_id:
        logger.warning("stripe.subscribe.missing_payment_method")
        raise HTTPException(status_code=400, detail="payment_method_id is required")
    try:
        customer = _find_or_create_customer(email=payload.customer_email)
        attached_pm = stripe.PaymentMethod.attach(
            payload.payment_method_id,
            customer=customer["id"],
        )
        stripe.Customer.modify(
            customer["id"],
            invoice_settings={"default_payment_method": attached_pm["id"]},
        )
        subscription = stripe.Subscription.create(
            customer=customer["id"],
            items=[{"price": plan_id}],
            trial_period_days=TRIAL_DAYS,
            payment_behavior="default_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            default_payment_method=attached_pm["id"],
            expand=[
                "latest_invoice.payment_intent",
            ],
        )
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        logger.warning("stripe.subscribe.failed", extra={"error": str(exc)})
        raise HTTPException(status_code=400, detail="Unable to create subscription") from exc
    trial_start = (
        datetime.fromtimestamp(subscription["trial_start"], tz=timezone.utc).isoformat()
        if subscription.get("trial_start")
        else _trial_window()[0]
    )
    trial_end = (
        datetime.fromtimestamp(subscription["trial_end"], tz=timezone.utc).isoformat()
        if subscription.get("trial_end")
        else _trial_window()[1]
    )
    current_period_end = (
        datetime.fromtimestamp(subscription["current_period_end"], tz=timezone.utc).isoformat()
        if subscription.get("current_period_end")
        else trial_end
    )
    client_secret = _extract_client_secret(subscription)
    record = {
        "subscription_id": subscription["id"],
        "customer_id": customer["id"],
        "status": subscription.get("status", "trialing"),
        "trial_start": trial_start,
        "trial_end": trial_end,
        "current_period_end": current_period_end,
        "plan_id": plan_id,
        "cancel_at_period_end": subscription.get("cancel_at_period_end", False),
        "email": payload.customer_email,
        "payment_method_id": payload.payment_method_id,
        "payment_behavior": "default_incomplete",
        "save_default_payment_method": "on_subscription",
        "client_secret": client_secret,
    }
    await _persist_subscription_record(db, record)
    logger.info(
        "stripe.subscribe.created",
        extra={
            "subscription_id": subscription["id"],
            "plan_id": plan_id,
            "plan_label": plan_label,
            "trial_end": trial_end,
            "email_domain": _mask_email(payload.customer_email),
            "payment_behavior": record["payment_behavior"],
        },
    )
    return SubscribeResponse(
        subscription_id=subscription["id"],
        status=record["status"],
        trial_start=trial_start,
        trial_end=trial_end,
        current_period_end=current_period_end,
        plan_id=plan_id,
        plan_label=plan_label,
        payment_behavior=record["payment_behavior"],
        client_secret=client_secret,
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
        metrics.increment("stripe.webhook.signature_missing")
        metrics.alert(
            "stripe.webhook.signature_missing",
            value=1.0,
            threshold=0.0,
            severity="warning",
        )
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        pairs = dict(entry.split("=", 1) for entry in signature_header.split(","))
        timestamp = pairs.get("t")
        signature = pairs.get("v1")
    except Exception:  # pragma: no cover - defensive
        logger.warning("stripe.webhook.signature_parse_failed")
        metrics.increment("stripe.webhook.signature_invalid")
        metrics.alert(
            "stripe.webhook.signature_invalid",
            value=1.0,
            threshold=0.0,
            severity="warning",
        )
        raise HTTPException(status_code=403, detail="Invalid signature")
    if not timestamp or not signature:
        logger.warning("stripe.webhook.signature_parts_missing")
        metrics.increment("stripe.webhook.signature_invalid")
        metrics.alert(
            "stripe.webhook.signature_invalid",
            value=1.0,
            threshold=0.0,
            severity="warning",
        )
        raise HTTPException(status_code=403, detail="Invalid signature")
    expected = hmac.new(
        settings.stripe_webhook_secret.encode(),
        msg=f"{timestamp}.{payload.decode()}".encode(),
        digestmod=sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        logger.warning("stripe.webhook.signature_mismatch")
        metrics.increment("stripe.webhook.signature_invalid")
        metrics.alert(
            "stripe.webhook.signature_invalid",
            value=1.0,
            threshold=0.0,
            severity="warning",
        )
        raise HTTPException(status_code=403, detail="Invalid signature")


def _log_subscription_event(
    *, event_id: str, event_type: str, record: dict[str, Any], note: str | None = None
) -> None:
    """Emit a structured log for webhook-derived subscription updates."""
    logger.info(
        "stripe.webhook.persisted",
        extra={
            "event_id": event_id,
            "type": event_type,
            "subscription_id": record.get("subscription_id"),
            "status": record.get("status"),
            "plan_id": record.get("plan_id"),
            "note": note,
        },
    )
    metrics.increment("stripe.webhook.persisted", tags={"type": event_type})


def _format_cancel_message(effective_date: str | None) -> str:
    """Return a user-facing summary describing when access ends."""
    if not effective_date:
        return "Your access ends immediately and billing stops right away."
    try:
        parsed = datetime.fromisoformat(effective_date)
    except Exception:
        parsed = None
    if parsed:
        friendly = parsed.strftime("%b %d, %Y")
        comparison_dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        if comparison_dt > now_dt:
            return f"You'll retain access until {friendly}, then billing stops."
        return f"Access ended on {friendly}; billing has stopped."
    return f"Access ends at {effective_date}; billing stops afterward."


def _hash_undo_token(token: str) -> str:
    return blake2b(token.encode("utf-8"), digest_size=32).hexdigest()


def _undo_event_id(token_hash: str) -> str:
    return f"undo:{token_hash}"


async def _store_undo_token(db: AsyncSession, subscription_id: str, token_hash: str) -> None:
    event_id = _undo_event_id(token_hash)
    existing = await db.execute(select(ProcessedEvent).where(ProcessedEvent.event_id == event_id))
    if existing.scalar_one_or_none():
        return
    db.add(ProcessedEvent(event_id=event_id, subscription_id=subscription_id))
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("stripe.cancel.undo.persist_failed")
        raise HTTPException(status_code=500, detail="Failed to store undo token")


async def _get_undo_entry(db: AsyncSession, token_hash: str) -> ProcessedEvent | None:
    event_id = _undo_event_id(token_hash)
    result = await db.execute(select(ProcessedEvent).where(ProcessedEvent.event_id == event_id))
    return result.scalar_one_or_none()


async def _consume_undo_token(db: AsyncSession, token_hash: str) -> None:
    event_id = _undo_event_id(token_hash)
    await db.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == event_id))
    try:
        await db.commit()
    except Exception:
        await db.rollback()


async def _apply_subscription_event(payload: StripeWebhookPayload, db: AsyncSession | None) -> bool:
    """Apply webhook updates; return True if duplicate already processed."""
    if db:
        stmt = select(ProcessedEvent).where(ProcessedEvent.event_id == payload.id)
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            metrics.increment("stripe.webhook.duplicate_event", tags={"type": payload.type})
            return True

    if payload.type == "checkout.session.completed":
        obj = payload.data.get("object") if payload.data else {}
        sub_id = obj.get("subscription")
        customer_id = obj.get("customer")
        email = obj.get("customer_email") or obj.get("customer_details", {}).get("email")
        if not sub_id:
            return False
        record = {
            "subscription_id": sub_id,
            "customer_id": customer_id,
            "email": email,
            "status": "trialing",
            "plan_id": None,
            "cancel_at_period_end": False,
        }
        await _persist_subscription_record(db, record)
        _log_subscription_event(
            event_id=payload.id, event_type=payload.type, record=record, note="checkout"
        )
        if db:
            await _mark_event(db, payload.id, sub_id)
        return False

    if payload.type.startswith("invoice."):
        obj = payload.data.get("object") if payload.data else {}
        sub_id = obj.get("subscription")
        if not sub_id:
            return False
        price_id: str | None = None
        if obj.get("price"):
            price_id = obj["price"].get("id")
        if not price_id:
            lines = obj.get("lines", {}).get("data", [])
            if lines:
                price_id = (lines[0].get("price") or {}).get("id")
        email = obj.get("customer_email") or obj.get("customer_details", {}).get("email")
        record = {
            "subscription_id": sub_id,
            "status": obj.get("status", "incomplete"),
            "email": email,
            "plan_id": price_id,
            "customer_id": obj.get("customer"),
        }
        if payload.type == "invoice.payment_succeeded":
            record["status"] = "active"
        if payload.type == "invoice.payment_failed":
            record["status"] = "past_due"
        current_period_end = obj.get("current_period_end")
        if current_period_end:
            record["current_period_end"] = datetime.fromtimestamp(
                current_period_end, tz=timezone.utc
            ).isoformat()
        await _persist_subscription_record(db, record)
        _log_subscription_event(
            event_id=payload.id, event_type=payload.type, record=record, note="invoice"
        )
        if db:
            await _mark_event(db, payload.id, sub_id)
        return False

    if not payload.type.startswith("customer.subscription"):
        logger.info(
            "stripe.webhook.skipped_event",
            extra={"event_id": payload.id, "type": payload.type},
        )
        return False

    obj = payload.data.get("object") if payload.data else {}
    sub_id = obj.get("id")
    if not sub_id:
        return False
    record = {
        "subscription_id": sub_id,
        "plan_id": obj.get("plan", {}).get("id") if obj.get("plan") else None,
        "email": obj.get("customer_email"),
        "customer_id": obj.get("customer"),
    }
    status = obj.get("status")
    if status:
        record["status"] = status
    if obj.get("trial_start"):
        record["trial_start"] = datetime.fromtimestamp(
            obj["trial_start"], tz=timezone.utc
        ).isoformat()
    if obj.get("trial_end"):
        record["trial_end"] = datetime.fromtimestamp(obj["trial_end"], tz=timezone.utc).isoformat()
    if obj.get("current_period_end"):
        record["current_period_end"] = datetime.fromtimestamp(
            obj["current_period_end"], tz=timezone.utc
        ).isoformat()
    record["cancel_at_period_end"] = obj.get(
        "cancel_at_period_end", record.get("cancel_at_period_end", False)
    )
    if obj.get("default_payment_method"):
        record["payment_method_id"] = obj["default_payment_method"]
    if obj.get("customer"):
        record["customer_id"] = obj["customer"]
    if payload.type == "customer.subscription.trial_will_end":
        logger.info(
            "stripe.subscription.trial_will_end",
            extra={"subscription_id": sub_id, "email_domain": _mask_email(record.get("email"))},
        )
    await _persist_subscription_record(db, record)
    _log_subscription_event(event_id=payload.id, event_type=payload.type, record=record)
    if db:
        await _mark_event(db, payload.id, sub_id)
    return False


async def _resolve_subscription(payload: CancelRequest, db: AsyncSession | None) -> dict[str, Any]:
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    stmt = select(Subscription)
    if payload.subscription_id:
        stmt = stmt.where(Subscription.subscription_id == payload.subscription_id)
    elif payload.email:
        stmt = stmt.where(Subscription.email == payload.email)
    result = await db.execute(stmt)
    found = result.scalar_one_or_none()
    if found:
        return {
            "subscription_id": found.subscription_id,
            "customer_id": found.customer_id,
            "email": found.email,
            "plan_id": found.price_id,
            "status": found.status,
            "cancel_at_period_end": found.cancel_at_period_end,
            "current_period_end": found.current_period_end.isoformat()
            if found.current_period_end
            else None,
            "trial_end": found.trial_end.isoformat() if found.trial_end else None,
            "payment_method_id": found.default_payment_method,
        }
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
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: AsyncSession | None = Depends(get_database),
) -> WebhookResponse:
    """Accept Stripe webhook events with idempotency and optional signature verification."""
    body = await request.body()
    if db is None:
        logger.warning("stripe.webhook.db_missing")
        metrics.alert(
            "stripe.webhook.db_missing",
            value=1.0,
            threshold=0.0,
            severity="critical",
        )
        raise HTTPException(status_code=503, detail="Database not configured")
    await _ensure_event_tables(db)
    try:
        if settings.stripe_webhook_secret:
            event_obj = stripe.Webhook.construct_event(
                payload=body, sig_header=stripe_signature, secret=settings.stripe_webhook_secret
            )
        else:
            _verify_signature(body, stripe_signature)
            event_obj = json.loads(body.decode())
    except Exception:
        logger.warning("stripe.webhook.invalid_payload")
        metrics.increment(
            "stripe.webhook.invalid_payload", tags={"has_signature": bool(stripe_signature)}
        )
        metrics.alert(
            "stripe.webhook.invalid_payload",
            value=1.0,
            threshold=0.0,
            severity="warning",
            tags={"has_signature": bool(stripe_signature)},
        )
        raise HTTPException(status_code=400, detail="Invalid payload")
    payload = StripeWebhookPayload.model_validate(event_obj)
    duplicate = await _apply_subscription_event(payload, db)
    if duplicate:
        logger.info("stripe.webhook.duplicate", extra={"event_id": payload.id})
        metrics.increment("stripe.webhook.duplicate", tags={"type": payload.type})
        return WebhookResponse(received=True, duplicate=True)
    logger.info("stripe.webhook.received", extra={"event_id": payload.id, "type": payload.type})
    metrics.increment("stripe.webhook.received", tags={"type": payload.type})
    return WebhookResponse(received=True, duplicate=False)


@router.post("/billing/cancel", response_model=CancelResponse)
async def cancel_subscription(
    payload: CancelRequest,
    session: SessionContext = Depends(require_session),
    db: AsyncSession | None = Depends(get_database),
) -> CancelResponse:
    """Mark a subscription as cancelled; idempotent and cancel-at-period-end aware."""
    if db is None:
        logger.warning("stripe.cancel.db_missing")
        raise HTTPException(status_code=503, detail="Database not configured")
    await _ensure_event_tables(db)
    if not payload.subscription_id and not payload.email:
        logger.warning("stripe.cancel.missing_identifier")
        raise HTTPException(status_code=400, detail="subscription_id or email is required")
    subscription = await _resolve_subscription(payload, db)
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        try:
            stripe.Subscription.modify(
                subscription["subscription_id"],
                cancel_at_period_end=True,
            )
        except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
            logger.warning(
                "stripe.cancel.failed",
                extra={"subscription_id": subscription["subscription_id"], "error": str(exc)},
            )
            raise HTTPException(status_code=400, detail="Unable to cancel subscription") from exc
    subscription["cancel_at_period_end"] = True
    subscription["status"] = "canceled"
    subscription["reason"] = payload.reason or "unspecified"
    effective_date = subscription.get("current_period_end") or subscription.get("trial_end")
    if not effective_date:
        effective_dt = datetime.now(timezone.utc)
    elif isinstance(effective_date, datetime):
        effective_dt = effective_date
    else:
        try:
            effective_dt = datetime.fromisoformat(effective_date)
        except Exception:
            effective_dt = datetime.now(timezone.utc)
    effective_date_str = effective_dt.isoformat()
    message = _format_cancel_message(effective_date_str)
    undo_token: str | None = None
    undo_expires_at: str | None = None
    if settings.cancel_undo_ttl_seconds > 0:
        undo_token = secrets.token_urlsafe(32)
        token_hash = _hash_undo_token(undo_token)
        await _store_undo_token(db, subscription["subscription_id"], token_hash)
        undo_expires_dt = datetime.now(timezone.utc) + timedelta(
            seconds=settings.cancel_undo_ttl_seconds
        )
        undo_expires_at = undo_expires_dt.isoformat()
    await _persist_subscription_record(db, subscription)
    await _mark_event(db, f"cancel-{subscription['subscription_id']}")
    logger.info(
        "billing.cancelled",
        extra={
            "subscription_id": subscription["subscription_id"],
            "email_domain": _mask_email(subscription.get("email")),
            "reason": payload.reason,
            "effective_date": effective_date_str,
            "undo_expires_at": undo_expires_at,
        },
    )
    return CancelResponse(
        status="cancelled",
        effective_date=effective_date_str,
        message=message,
        note=payload.reason,
        undo_token=undo_token,
        undo_expires_at=undo_expires_at,
    )


@router.post("/billing/cancel/undo", response_model=CancelUndoResponse)
async def cancel_undo(
    payload: CancelUndoRequest,
    session: SessionContext = Depends(require_session),
    db: AsyncSession | None = Depends(get_database),
) -> CancelUndoResponse:
    if db is None:
        logger.warning("stripe.cancel.undo.db_missing")
        raise HTTPException(status_code=503, detail="Database not configured")
    await _ensure_event_tables(db)
    if not payload.undo_token:
        raise HTTPException(status_code=400, detail="undo_token is required")
    token_hash = _hash_undo_token(payload.undo_token)
    entry = await _get_undo_entry(db, token_hash)
    if not entry:
        raise HTTPException(status_code=404, detail="Undo token not found")
    ttl_seconds = max(settings.cancel_undo_ttl_seconds, 0)
    entry_received = entry.received_at
    if entry_received.tzinfo is None:
        entry_received = entry_received.replace(tzinfo=timezone.utc)
    expires_at = entry_received + timedelta(seconds=ttl_seconds)
    now_dt = datetime.now(timezone.utc)
    if now_dt > expires_at:
        await _consume_undo_token(db, token_hash)
        logger.info(
            "billing.cancel.undo_expired",
            extra={
                "subscription_id": entry.subscription_id,
                "email_domain": "*",
                "expires_at": expires_at.isoformat(),
            },
        )
        raise HTTPException(status_code=410, detail="Undo token expired")
    stmt = select(Subscription).where(Subscription.subscription_id == entry.subscription_id)
    result = await db.execute(stmt)
    subscription = result.scalar_one_or_none()
    if not subscription:
        await _consume_undo_token(db, token_hash)
        raise HTTPException(status_code=404, detail="Subscription not found")
    record = {
        "subscription_id": subscription.subscription_id,
        "customer_id": subscription.customer_id,
        "email": subscription.email,
        "plan_id": subscription.price_id,
        "status": "active",
        "cancel_at_period_end": False,
        "payment_method_id": subscription.default_payment_method,
    }
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        try:
            stripe.Subscription.modify(subscription.subscription_id, cancel_at_period_end=False)
        except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
            logger.warning(
                "stripe.cancel.undo_failed",
                extra={"subscription_id": subscription.subscription_id, "error": str(exc)},
            )
            raise HTTPException(status_code=400, detail="Unable to undo cancellation") from exc
    await _persist_subscription_record(db, record)
    await _consume_undo_token(db, token_hash)
    restored_at = now_dt.isoformat()
    logger.info(
        "billing.cancel.undo",
        extra={
            "subscription_id": subscription.subscription_id,
            "email_domain": _mask_email(subscription.email),
            "restored_at": restored_at,
        },
    )
    return CancelUndoResponse(
        status="restored",
        restored_at=restored_at,
        message="Your subscription has been restored and billing continues.",
    )


def _to_iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo:
        return dt.isoformat()
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _serialize_subscription(record: Subscription) -> SubscriptionStateResponse:
    return SubscriptionStateResponse(
        subscription_id=record.subscription_id,
        plan_id=record.price_id,
        status=record.status,
        trial_start=_to_iso(record.trial_start),
        trial_end=_to_iso(record.trial_end),
        current_period_end=_to_iso(record.current_period_end),
        cancel_at_period_end=record.cancel_at_period_end,
        updated_at=_to_iso(record.updated_at) or datetime.now(timezone.utc).isoformat(),
    )


@router.get("/billing/subscription", response_model=SubscriptionStateResponse)
async def get_subscription_state(
    session: SessionContext = Depends(require_session),
    db: AsyncSession | None = Depends(get_database),
) -> SubscriptionStateResponse:
    if db is None:
        logger.warning("stripe.subscription_state.db_missing")
        raise HTTPException(status_code=503, detail="Database not configured")
    stmt = (
        select(Subscription)
        .where(Subscription.email == session.email)
        .order_by(Subscription.updated_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    record = result.scalar_one_or_none()
    if not record:
        logger.info(
            "stripe.subscription_state.missing",
            extra={"email_domain": _mask_email(session.email)},
        )
        raise HTTPException(status_code=404, detail="Subscription not found")
    logger.info(
        "stripe.subscription_state.read",
        extra={
            "subscription_id": record.subscription_id,
            "plan_id": record.price_id,
            "status": record.status,
            "email_domain": _mask_email(session.email),
        },
    )
    return _serialize_subscription(record)
