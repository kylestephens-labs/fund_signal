"""Authentication endpoints for magic links, OTP, and optional Google callback."""
# ruff: noqa: UP017, UP006, UP007, UP035

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from datetime import (  # noqa: UP017 - timezone.utc for py3.9 compatibility
    datetime,
    timedelta,
    timezone,
)
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

logger = logging.getLogger(__name__)
router = APIRouter()


MAGIC_LINK_TTL_SECONDS = 600
OTP_TTL_SECONDS = 600


@dataclass
class _TokenRecord:
    email: str
    expires_at: datetime
    used: bool
    token_type: str


_tokens: dict[str, _TokenRecord] = {}
_otp_codes: dict[str, _TokenRecord] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _generate_otp() -> str:
    alphabet = string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


class MagicLinkRequest(BaseModel):
    email: EmailStr


class MagicLinkResponse(BaseModel):
    message: str
    expires_in: int
    debug_token: Optional[str] = None


class MagicLinkVerifyRequest(BaseModel):
    token: str


class SessionResponse(BaseModel):
    status: str
    email: EmailStr
    subscription: Dict[str, str]


class OTPRequest(BaseModel):
    email: EmailStr


class OTPVerifyRequest(BaseModel):
    email: EmailStr
    otp: str


@router.post("/auth/magic-link", response_model=MagicLinkResponse, status_code=202)
async def request_magic_link(payload: MagicLinkRequest) -> MagicLinkResponse:
    """Issue a time-bound magic link token (email delivery handled elsewhere)."""
    token = _generate_token()
    _tokens[token] = _TokenRecord(  # noqa: S106 - token type is descriptive, not a secret
        email=payload.email,
        expires_at=_now() + timedelta(seconds=MAGIC_LINK_TTL_SECONDS),
        used=False,
        token_type="magic",  # noqa: S106 - descriptive marker
    )
    logger.info(
        "auth.magic_link.issued",
        extra={"email": payload.email, "expires_in": MAGIC_LINK_TTL_SECONDS},
    )
    debug_token = token if logger.isEnabledFor(logging.DEBUG) else None
    return MagicLinkResponse(
        message="sent", expires_in=MAGIC_LINK_TTL_SECONDS, debug_token=debug_token
    )


def _resolve_token(token: str, container: dict[str, _TokenRecord], token_type: str) -> _TokenRecord:
    record = container.get(token)
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    if record.used:
        raise HTTPException(status_code=409, detail="Token already used")
    if record.expires_at < _now():
        raise HTTPException(status_code=400, detail="Token expired")
    if record.token_type != token_type:
        raise HTTPException(status_code=400, detail="Token type mismatch")
    return record


@router.post("/auth/magic-link/verify", response_model=SessionResponse)
async def verify_magic_link(payload: MagicLinkVerifyRequest) -> SessionResponse:
    """Verify a magic link token and start a trial subscription."""
    record = _resolve_token(payload.token, _tokens, "magic")
    record.used = True
    subscription = {
        "status": "trialing",
        "trial_started_at": _now().isoformat(),
    }
    logger.info(
        "auth.magic_link.verified",
        extra={"email": record.email, "subscription_status": subscription["status"]},
    )
    return SessionResponse(status="verified", email=record.email, subscription=subscription)


@router.post("/auth/otp", response_model=MagicLinkResponse, status_code=202)
async def request_otp(payload: OTPRequest) -> MagicLinkResponse:
    """Issue a time-bound OTP code."""
    otp = _generate_otp()
    _otp_codes[otp] = _TokenRecord(  # noqa: S106 - token type is descriptive, not a secret
        email=payload.email,
        expires_at=_now() + timedelta(seconds=OTP_TTL_SECONDS),
        used=False,
        token_type="otp",  # noqa: S106 - descriptive marker
    )
    logger.info(
        "auth.otp.issued",
        extra={"email": payload.email, "expires_in": OTP_TTL_SECONDS},
    )
    debug_token = otp if logger.isEnabledFor(logging.DEBUG) else None
    return MagicLinkResponse(message="sent", expires_in=OTP_TTL_SECONDS, debug_token=debug_token)


@router.post("/auth/otp/verify", response_model=SessionResponse)
async def verify_otp(payload: OTPVerifyRequest) -> SessionResponse:
    """Verify an OTP code and start a trial subscription."""
    record = _resolve_token(payload.otp, _otp_codes, "otp")
    record.used = True
    subscription = {
        "status": "trialing",
        "trial_started_at": _now().isoformat(),
    }
    logger.info(
        "auth.otp.verified",
        extra={"email": record.email, "subscription_status": subscription["status"]},
    )
    return SessionResponse(status="verified", email=record.email, subscription=subscription)


class GoogleCallbackRequest(BaseModel):
    code: Optional[str] = None
    state: Optional[str] = None


class GoogleCallbackResponse(BaseModel):
    status: str
    message: str


@router.post("/auth/google/callback", response_model=GoogleCallbackResponse)
async def google_callback(_: GoogleCallbackRequest) -> GoogleCallbackResponse:
    """Stub endpoint for optional Google OAuth callback."""
    logger.info("auth.google.callback.stub")
    return GoogleCallbackResponse(
        status="not_enabled", message="Google OAuth not configured for backend flow."
    )
