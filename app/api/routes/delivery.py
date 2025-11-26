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
from uuid import UUID

try:  # Stripe optional for tests without dependency installed
    import stripe  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback stub
    from types import SimpleNamespace

    class _StripeError(Exception):
        pass

    stripe = SimpleNamespace(  # type: ignore[assignment]
        api_key=None,
        Webhook=SimpleNamespace(
            construct_event=lambda payload, sig_header, secret: json.loads(payload.decode())
        ),
        Customer=SimpleNamespace(
            list=lambda email, limit=1: SimpleNamespace(data=[]),
            create=lambda email: {"id": "cus_test", "email": email},
            modify=lambda customer_id, invoice_settings=None: {
                "id": customer_id,
                "invoice_settings": invoice_settings,
            },
        ),
        PaymentMethod=SimpleNamespace(
            attach=lambda pm_id, customer=None: {"id": pm_id, "customer": customer}
        ),
        Subscription=SimpleNamespace(
            create=lambda **kwargs: {"id": "sub_test", "status": "trialing"},
            modify=lambda *args, **kwargs: {},
        ),
        error=SimpleNamespace(StripeError=_StripeError),
    )
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
_subscriptions_path = Path(settings.delivery_output_dir or "output") / "subscriptions.json"

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
    payment_behavior: str
    client_secret: str | None = None


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


def _extract_client_secret(subscription: dict[str, Any]) -> str | None:
    """Return payment_intent or setup_intent client secret if present."""
    latest_invoice = subscription.get("latest_invoice")
    invoice_obj = latest_invoice if isinstance(latest_invoice, dict) else {}
    intent = invoice_obj.get("payment_intent") or invoice_obj.get("setup_intent")
    if isinstance(intent, dict):
        return intent.get("client_secret")
    return None


def _load_subscriptions() -> dict[str, dict[str, Any]]:
    if _subscriptions:
        return _subscriptions
    if _subscriptions_path.exists():
        try:
            payload = json.loads(_subscriptions_path.read_text(encoding="utf-8"))
            _subscriptions.update(payload)
        except Exception:
            logger.warning("stripe.subscriptions.load_failed")
    return _subscriptions


def _persist_subscriptions() -> None:
    try:
        _subscriptions_path.parent.mkdir(parents=True, exist_ok=True)
        _subscriptions_path.write_text(json.dumps(_subscriptions), encoding="utf-8")
    except Exception:
        logger.warning("stripe.subscriptions.persist_failed")


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
    payload: SubscribeRequest, session: SessionContext = Depends(require_session)
) -> SubscribeResponse:
    """Create a subscription with a 14-day trial; no immediate charge."""
    _load_subscriptions()
    if not settings.stripe_secret_key:
        logger.warning("stripe.subscribe.missing_secret")
        raise HTTPException(status_code=503, detail="Stripe not configured")
    stripe.api_key = settings.stripe_secret_key
    plan_id = payload.resolved_plan()
    if not payload.payment_method_id:
        logger.warning("stripe.subscribe.missing_payment_method")
        raise HTTPException(status_code=400, detail="payment_method_id is required")
    try:
        customer = _find_or_create_customer(email=payload.customer_email)
        stripe.PaymentMethod.attach(
            payload.payment_method_id,
            customer=customer["id"],
        )
        stripe.Customer.modify(
            customer["id"],
            invoice_settings={"default_payment_method": payload.payment_method_id},
        )
        subscription = stripe.Subscription.create(
            customer=customer["id"],
            items=[{"price": plan_id}],
            trial_period_days=TRIAL_DAYS,
            payment_behavior="default_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            default_payment_method=payload.payment_method_id,
            expand=[
                "latest_invoice.payment_intent",
                "latest_invoice.payment_intent.client_secret",
                "latest_invoice.setup_intent",
                "latest_invoice.setup_intent.client_secret",
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
    _subscriptions[subscription["id"]] = record
    _persist_subscriptions()
    logger.info(
        "stripe.subscribe.created",
        extra={
            "subscription_id": subscription["id"],
            "plan_id": plan_id,
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
    if payload.type.startswith("invoice."):
        obj = payload.data.get("object") if payload.data else {}
        sub_id = obj.get("subscription")
        if not sub_id:
            return
        record = _subscriptions.setdefault(
            sub_id,
            {
                "subscription_id": sub_id,
                "status": obj.get("status", "incomplete"),
                "email": obj.get("customer_email"),
            },
        )
        if payload.type == "invoice.payment_succeeded":
            record["status"] = "active"
        if payload.type == "invoice.payment_failed":
            record["status"] = "past_due"
        current_period_end = obj.get("current_period_end")
        if current_period_end:
            record["current_period_end"] = datetime.fromtimestamp(
                current_period_end, tz=timezone.utc
            ).isoformat()
        _subscriptions[sub_id] = record
        return

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
    _load_subscriptions()
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
        raise HTTPException(status_code=400, detail="Invalid payload")
    payload = StripeWebhookPayload.model_validate(event_obj)
    if payload.id in _webhook_events:
        logger.info("stripe.webhook.duplicate", extra={"event_id": payload.id})
        return WebhookResponse(received=True, duplicate=True)
    _webhook_events.add(payload.id)
    _apply_subscription_event(payload)
    _persist_subscriptions()
    logger.info("stripe.webhook.received", extra={"event_id": payload.id, "type": payload.type})
    return WebhookResponse(received=True, duplicate=False)


@router.post("/billing/cancel", response_model=CancelResponse)
async def cancel_subscription(
    payload: CancelRequest, session: SessionContext = Depends(require_session)
) -> CancelResponse:
    """Mark a subscription as cancelled; idempotent and cancel-at-period-end aware."""
    _load_subscriptions()
    subscription = _resolve_subscription(payload)
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
        effective_date = datetime.now(timezone.utc).isoformat()
    _persist_subscriptions()
    logger.info(
        "billing.cancelled",
        extra={
            "subscription_id": subscription["subscription_id"],
            "email_domain": _mask_email(subscription.get("email")),
            "reason": payload.reason,
        },
    )
    return CancelResponse(status="cancelled", effective_date=effective_date, note=payload.reason)
