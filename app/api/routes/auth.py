"""Authentication endpoints for magic links, OTP, and optional Google callback."""
# ruff: noqa: UP017, UP006, UP007, UP035

from __future__ import annotations

import logging
import secrets
import string
import time
from dataclasses import dataclass
from datetime import (  # noqa: UP017 - timezone.utc for py3.9 compatibility
    datetime,
    timedelta,
    timezone,
)
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


MAGIC_LINK_TTL_SECONDS = 600
OTP_TTL_SECONDS = 600
RATE_LIMIT_KEY_MAGIC = "magic"
RATE_LIMIT_KEY_OTP = "otp"


@dataclass
class _TokenRecord:
    email: str
    expires_at: datetime
    used: bool
    token_type: str
    plan_id: str | None


_tokens: dict[str, _TokenRecord] = {}
_otp_codes: dict[str, _TokenRecord] = {}


class RateLimiter:
    """In-memory rate limiter keyed by (identity, token_type)."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[tuple[str, str], list[float]] = {}

    def check(self, key: tuple[str, str]) -> float | None:
        """Record a hit; return retry_after seconds if rate-limited."""
        now = time.monotonic()
        window_start = now - self.window_seconds
        bucket = self._requests.setdefault(key, [])
        # drop expired hits
        while bucket and bucket[0] < window_start:
            bucket.pop(0)
        if len(bucket) >= self.max_requests:
            retry_after = bucket[0] + self.window_seconds - now
            return max(retry_after, 0.0)
        bucket.append(now)
        return None

    def reset(self) -> None:
        self._requests.clear()


_rate_limiter = RateLimiter(
    max_requests=settings.auth_rate_limit_max_requests,
    window_seconds=settings.auth_rate_limit_window_seconds,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _generate_otp() -> str:
    alphabet = string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


class MagicLinkRequest(BaseModel):
    email: EmailStr
    plan_id: Optional[str] = None


class MagicLinkResponse(BaseModel):
    message: str
    expires_in: int
    debug_token: Optional[str] = None


class MagicLinkVerifyRequest(BaseModel):
    token: str


class Subscription(BaseModel):
    status: str
    trial_started_at: str
    plan_id: Optional[str] = None


class SessionResponse(BaseModel):
    status: str
    email: EmailStr
    subscription: Subscription


class OTPRequest(BaseModel):
    email: EmailStr
    plan_id: Optional[str] = None


class OTPVerifyRequest(BaseModel):
    email: EmailStr
    otp: str


@router.post("/auth/magic-link", response_model=MagicLinkResponse, status_code=202)
async def request_magic_link(payload: MagicLinkRequest) -> MagicLinkResponse:
    """Issue a time-bound magic link token (email delivery handled elsewhere)."""
    plan_id = _validate_plan(payload.plan_id)
    _enforce_rate_limit(identity=payload.email, token_type=RATE_LIMIT_KEY_MAGIC)
    token = _generate_token()
    _tokens[token] = _TokenRecord(  # noqa: S106 - token type is descriptive, not a secret
        email=payload.email,
        expires_at=_now() + timedelta(seconds=MAGIC_LINK_TTL_SECONDS),
        used=False,
        token_type="magic",  # noqa: S106 - descriptive marker
        plan_id=plan_id,
    )
    logger.info(
        "auth.magic_link.issued",
        extra={
            "email_domain": _mask_email(payload.email),
            "expires_in": MAGIC_LINK_TTL_SECONDS,
            "plan_id": plan_id,
        },
    )
    debug_token = token if logger.isEnabledFor(logging.DEBUG) else None
    return MagicLinkResponse(
        message="sent", expires_in=MAGIC_LINK_TTL_SECONDS, debug_token=debug_token
    )


def _resolve_token(token: str, container: dict[str, _TokenRecord], token_type: str) -> _TokenRecord:
    record = container.get(token)
    if not record:
        logger.warning("auth.token.invalid", extra={"token_type": token_type})
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    if record.used:
        logger.warning(
            "auth.token.already_used",
            extra={"token_type": token_type, "email_domain": _mask_email(record.email)},
        )
        raise HTTPException(status_code=409, detail="Token already used")
    if record.expires_at < _now():
        logger.warning(
            "auth.token.expired",
            extra={"token_type": token_type, "email_domain": _mask_email(record.email)},
        )
        raise HTTPException(status_code=400, detail="Token expired")
    if record.token_type != token_type:
        logger.warning(
            "auth.token.type_mismatch",
            extra={"token_type": token_type, "email_domain": _mask_email(record.email)},
        )
        raise HTTPException(status_code=400, detail="Token type mismatch")
    return record


@router.post("/auth/magic-link/verify", response_model=SessionResponse)
async def verify_magic_link(payload: MagicLinkVerifyRequest) -> SessionResponse:
    """Verify a magic link token and start a trial subscription."""
    record = _resolve_token(payload.token, _tokens, "magic")
    record.used = True
    subscription = Subscription(
        status="trialing",
        trial_started_at=_now().isoformat(),
        plan_id=record.plan_id,
    )
    logger.info(
        "auth.magic_link.verified",
        extra={
            "email_domain": _mask_email(record.email),
            "subscription_status": subscription.status,
            "plan_id": record.plan_id,
        },
    )
    return SessionResponse(status="verified", email=record.email, subscription=subscription)


@router.post("/auth/otp", response_model=MagicLinkResponse, status_code=202)
async def request_otp(payload: OTPRequest) -> MagicLinkResponse:
    """Issue a time-bound OTP code."""
    plan_id = _validate_plan(payload.plan_id)
    _enforce_rate_limit(identity=payload.email, token_type=RATE_LIMIT_KEY_OTP)
    otp = _generate_otp()
    _otp_codes[otp] = _TokenRecord(  # noqa: S106 - token type is descriptive, not a secret
        email=payload.email,
        expires_at=_now() + timedelta(seconds=OTP_TTL_SECONDS),
        used=False,
        token_type="otp",  # noqa: S106 - descriptive marker
        plan_id=plan_id,
    )
    logger.info(
        "auth.otp.issued",
        extra={
            "email_domain": _mask_email(payload.email),
            "expires_in": OTP_TTL_SECONDS,
            "plan_id": plan_id,
        },
    )
    debug_token = otp if logger.isEnabledFor(logging.DEBUG) else None
    return MagicLinkResponse(message="sent", expires_in=OTP_TTL_SECONDS, debug_token=debug_token)


@router.post("/auth/otp/verify", response_model=SessionResponse)
async def verify_otp(payload: OTPVerifyRequest) -> SessionResponse:
    """Verify an OTP code and start a trial subscription."""
    record = _resolve_token(payload.otp, _otp_codes, "otp")
    if record.email != payload.email:
        logger.warning(
            "auth.otp.email_mismatch",
            extra={"email_domain": _mask_email(payload.email)},
        )
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    record.used = True
    subscription = Subscription(
        status="trialing",
        trial_started_at=_now().isoformat(),
        plan_id=record.plan_id,
    )
    logger.info(
        "auth.otp.verified",
        extra={
            "email_domain": _mask_email(record.email),
            "subscription_status": subscription.status,
            "plan_id": record.plan_id,
        },
    )
    return SessionResponse(status="verified", email=record.email, subscription=subscription)


class GoogleCallbackRequest(BaseModel):
    code: Optional[str] = None
    state: Optional[str] = None


class GoogleCallbackResponse(BaseModel):
    status: str
    message: str
    state: Optional[str] = None


@router.post("/auth/google/callback", response_model=GoogleCallbackResponse)
async def google_callback(_: GoogleCallbackRequest) -> GoogleCallbackResponse:
    """Stub endpoint for optional Google OAuth callback."""
    if not settings.google_client_id or not settings.google_client_secret:
        logger.info("auth.google.callback.not_enabled")
        return GoogleCallbackResponse(
            status="not_enabled", message="Google OAuth not configured for backend flow."
        )
    logger.info("auth.google.callback.unimplemented")
    return GoogleCallbackResponse(
        status="pending",
        message="Google OAuth not implemented in backend flow.",
        state=_.state,
    )


def _validate_plan(plan_id: str | None) -> str | None:
    if plan_id is None:
        return None
    allowed = settings.auth_allowed_plans
    if all(entry.startswith("price_") for entry in allowed):
        plan = plan_id.strip()
    else:
        plan = plan_id.strip().lower()
    if plan not in allowed:
        logger.warning("auth.plan.invalid", extra={"plan_id": plan})
        raise HTTPException(status_code=400, detail="Invalid plan")
    return plan


def _mask_email(email: str) -> str:
    domain = email.split("@")[-1] if "@" in email else ""
    return f"*@{domain}" if domain else "*"


def _enforce_rate_limit(*, identity: str, token_type: str) -> None:
    retry_after = _rate_limiter.check((identity.lower(), token_type))
    if retry_after is not None:
        logger.warning(
            "auth.rate_limited",
            extra={
                "email_domain": _mask_email(identity),
                "token_type": token_type,
                "retry_after": retry_after,
            },
        )
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please retry later.",
            headers={"Retry-After": f"{int(retry_after)}"},
        )
