"""Authentication endpoints for magic links, OTP, and optional Google callback."""
# ruff: noqa: UP017, UP006, UP007, UP035

from __future__ import annotations

import logging
import secrets
import smtplib
import string
import time
from dataclasses import dataclass
from datetime import (  # noqa: UP017 - timezone.utc for py3.9 compatibility
    datetime,
    timedelta,
    timezone,
)
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, EmailStr

from app.config import settings
from app.observability.metrics import metrics

logger = logging.getLogger(__name__)
router = APIRouter()


MAGIC_LINK_TTL_SECONDS = 600
OTP_TTL_SECONDS = 600
GOOGLE_STATE_TTL_SECONDS = 600
RATE_LIMIT_KEY_MAGIC = "magic"
RATE_LIMIT_KEY_OTP = "otp"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - provider URL, not a credential
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


@dataclass
class _TokenRecord:
    email: str
    expires_at: datetime
    used: bool
    token_type: str
    plan_id: str | None


@dataclass
class _GoogleState:
    plan_id: str | None
    expires_at: datetime


_tokens: dict[str, _TokenRecord] = {}
_otp_codes: dict[str, _TokenRecord] = {}
_google_states: dict[str, _GoogleState] = {}

SESSION_TTL_SECONDS = 3600


@dataclass
class SessionContext:
    token: str
    email: str
    expires_at: datetime
    plan_id: str | None


_sessions: dict[str, SessionContext] = {}
_opt_out_emails: set[str] = set()
_unlock_sent: set[str] = set()


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
    session_token: str
    opted_out: bool


class OTPRequest(BaseModel):
    email: EmailStr
    plan_id: Optional[str] = None


class OTPVerifyRequest(BaseModel):
    email: EmailStr
    otp: str


class OptOutRequest(BaseModel):
    email: EmailStr
    opt_out: bool = True
    reason: str | None = None


class OptOutResponse(BaseModel):
    status: str
    opted_out: bool


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
async def verify_magic_link(
    payload: MagicLinkVerifyRequest, background_tasks: BackgroundTasks
) -> SessionResponse:
    """Verify a magic link token and start a trial subscription."""
    record = _resolve_token(payload.token, _tokens, "magic")
    record.used = True
    subscription = Subscription(
        status="trialing",
        trial_started_at=_now().isoformat(),
        plan_id=record.plan_id,
    )
    session = _issue_session(email=record.email, plan_id=record.plan_id)
    _schedule_unlock_email(session, background_tasks)
    logger.info(
        "auth.magic_link.verified",
        extra={
            "email_domain": _mask_email(record.email),
            "subscription_status": subscription.status,
            "plan_id": record.plan_id,
        },
    )
    return SessionResponse(
        status="verified",
        email=record.email,
        subscription=subscription,
        session_token=session.token,
        opted_out=_is_opted_out(record.email),
    )


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
async def verify_otp(
    payload: OTPVerifyRequest, background_tasks: BackgroundTasks
) -> SessionResponse:
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
    session = _issue_session(email=record.email, plan_id=record.plan_id)
    _schedule_unlock_email(session, background_tasks)
    logger.info(
        "auth.otp.verified",
        extra={
            "email_domain": _mask_email(record.email),
            "subscription_status": subscription.status,
            "plan_id": record.plan_id,
        },
    )
    return SessionResponse(
        status="verified",
        email=record.email,
        subscription=subscription,
        session_token=session.token,
        opted_out=_is_opted_out(record.email),
    )


class GoogleCallbackRequest(BaseModel):
    code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None


class GoogleCallbackResponse(BaseModel):
    status: str
    message: str
    state: Optional[str] = None
    email: Optional[EmailStr] = None
    subscription: Optional[Subscription] = None
    session_token: Optional[str] = None
    opted_out: bool | None = None


class GoogleAuthUrlResponse(BaseModel):
    url: str
    state: str


@router.post("/auth/google/callback", response_model=GoogleCallbackResponse)
async def google_callback(
    _: GoogleCallbackRequest, background_tasks: BackgroundTasks
) -> GoogleCallbackResponse:
    """Handle Google OAuth callback: exchange code, fetch user, return session."""
    if (
        not settings.google_client_id
        or not settings.google_client_secret
        or not settings.google_redirect_uri
    ):
        logger.info("auth.google.callback.not_enabled")
        return GoogleCallbackResponse(
            status="not_enabled", message="Google OAuth not configured for backend flow."
        )
    if _.error:
        logger.warning("auth.google.callback.error", extra={"error": _.error})
        raise HTTPException(status_code=400, detail="Google OAuth error")
    if not _.code:
        logger.warning("auth.google.callback.missing_code")
        raise HTTPException(status_code=400, detail="Missing authorization code")

    state = _resolve_google_state(_.state)
    token_payload = {
        "code": _.code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            token_resp = await client.post(GOOGLE_TOKEN_URL, data=token_payload)
            token_resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("auth.google.token_exchange_failed", extra={"error": str(exc)})
            raise HTTPException(status_code=400, detail="Google token exchange failed") from exc
        tokens = token_resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            logger.warning("auth.google.missing_access_token")
            raise HTTPException(status_code=400, detail="Google token exchange failed")
        try:
            userinfo_resp = await client.get(
                GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            userinfo_resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("auth.google.userinfo_failed", extra={"error": str(exc)})
            raise HTTPException(status_code=400, detail="Google userinfo fetch failed") from exc
        userinfo = userinfo_resp.json()
    email = userinfo.get("email")
    if not email:
        logger.warning("auth.google.email_missing")
        raise HTTPException(status_code=400, detail="Google account missing email")

    subscription = Subscription(
        status="trialing",
        trial_started_at=_now().isoformat(),
        plan_id=state.plan_id,
    )
    session = _issue_session(email=email, plan_id=state.plan_id)
    _schedule_unlock_email(session, background_tasks)
    logger.info(
        "auth.google.callback.success",
        extra={
            "email_domain": _mask_email(email),
            "plan_id": state.plan_id,
        },
    )
    return GoogleCallbackResponse(
        status="verified",
        message="Google OAuth verified",
        state=_.state,
        email=email,
        subscription=subscription,
        session_token=session.token,
        opted_out=_is_opted_out(email),
    )


def _validate_plan(plan_id: str | None) -> str | None:
    if plan_id is None:
        return None
    allowed = settings.auth_allowed_plans | {"solo", "growth", "team"}
    plan = plan_id.strip()
    plan_normalized = plan.lower()
    # Map legacy "starter" to "solo" for backward compatibility
    if plan_normalized == "starter":
        plan_normalized = "solo"
    if plan_normalized in {"solo", "growth", "team"}:
        plan = plan_normalized
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


def _issue_google_state(plan_id: str | None) -> str:
    state = secrets.token_urlsafe(32)
    _google_states[state] = _GoogleState(
        plan_id=plan_id,
        expires_at=_now() + timedelta(seconds=GOOGLE_STATE_TTL_SECONDS),
    )
    return state


def _resolve_google_state(state: str | None) -> _GoogleState:
    if not state:
        logger.warning("auth.google.state.missing")
        raise HTTPException(status_code=400, detail="Invalid state")
    record = _google_states.pop(state, None)
    if not record:
        logger.warning("auth.google.state.unknown")
        raise HTTPException(status_code=400, detail="Invalid state")
    if record.expires_at < _now():
        logger.warning("auth.google.state.expired")
        raise HTTPException(status_code=400, detail="State expired")
    return record


def _issue_session(*, email: str, plan_id: str | None) -> SessionContext:
    token = _generate_token()
    ctx = SessionContext(
        token=token,
        email=email,
        plan_id=plan_id,
        expires_at=_now() + timedelta(seconds=SESSION_TTL_SECONDS),
    )
    _sessions[token] = ctx
    logger.info(
        "auth.session.issued",
        extra={
            "email_domain": _mask_email(email),
            "plan_id": plan_id,
            "expires_in": SESSION_TTL_SECONDS,
        },
    )
    return ctx


def _is_opted_out(email: str) -> bool:
    return email.lower() in _opt_out_emails


def _resolve_session(token: str) -> SessionContext:
    ctx = _sessions.get(token)
    if not ctx:
        logger.warning("auth.session.invalid")
        raise HTTPException(status_code=401, detail="Invalid session")
    if ctx.expires_at < _now():
        logger.warning("auth.session.expired", extra={"email_domain": _mask_email(ctx.email)})
        raise HTTPException(status_code=401, detail="Session expired")
    return ctx


def require_session(authorization: str | None = Header(default=None)) -> SessionContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("auth.session.missing_header")
        raise HTTPException(status_code=401, detail="Missing or invalid authorization")
    token = authorization.split(" ", 1)[1].strip()
    return _resolve_session(token)


def _schedule_unlock_email(session: SessionContext, background_tasks: BackgroundTasks) -> None:
    """Dispatch the unlock email after verification without blocking the request."""
    email_key = session.email.lower()
    if _is_opted_out(email_key):
        logger.info(
            "delivery.unlock.skipped_opt_out", extra={"email_domain": _mask_email(session.email)}
        )
        metrics.increment("delivery.unlock.skipped", tags={"reason": "opt_out"})
        return
    if email_key in _unlock_sent:
        logger.info(
            "delivery.unlock.already_sent", extra={"email_domain": _mask_email(session.email)}
        )
        metrics.increment("delivery.unlock.skipped", tags={"reason": "duplicate"})
        return
    metrics.increment("delivery.unlock.queued")
    background_tasks.add_task(_dispatch_unlock_email, session)


def _dispatch_unlock_email(session: SessionContext) -> None:
    """Render and persist a full report email artifact."""
    email_key = session.email.lower()
    logger.info(
        "delivery.unlock.dispatch_start", extra={"email_domain": _mask_email(session.email)}
    )
    metrics.increment("delivery.unlock.dispatch_start")
    try:
        from app.api.routes import delivery as delivery_routes

        leads = delivery_routes._load_fixture()
    except Exception:  # pragma: no cover - defensive
        logger.exception("delivery.unlock.failed_to_load_leads")
        metrics.alert(
            "delivery.unlock.dispatch_failed",
            value=1.0,
            threshold=0.0,
            severity="critical",
            tags={"reason": "load_fixture"},
        )
        return
    output_dir = Path(settings.delivery_output_dir or "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = _now().isoformat()
    body_lines = [
        f"# FundSignal Full Report — {session.email}",
        "",
        f"_Generated at {generated_at}_",
        "",
        "Upgrade now to keep weekly deliveries coming.",
        "",
    ]
    for idx, lead in enumerate(leads[:50], start=1):
        proofs = lead.get("proofs") or []
        body_lines.append(
            f"{idx}. {lead.get('company_id')} — {lead.get('score')} pts (freshness: {lead.get('freshness', 'unknown')})"
        )
        body_lines.append(f"   - Recommended: {lead.get('recommended_approach')}")
        body_lines.append(f"   - Pitch: {lead.get('pitch_angle')}")
        if proofs:
            for proof in proofs:
                body_lines.append(f"   - Proof: {proof}")
        body_lines.append("")
    body_lines.append("To opt out of emails, call POST /auth/opt-out with your email.")
    artifact = output_dir / f"unlock_email_{session.token}.md"
    artifact.write_text("\n".join(body_lines), encoding="utf-8")
    _unlock_sent.add(email_key)
    lead_count = len(leads)
    metrics.increment("delivery.unlock.artifact_written", tags={"lead_count": lead_count})
    delivery_status = _maybe_email_unlock(session.email, body_lines)
    logger.info(
        "delivery.unlock.sent",
        extra={
            "email_domain": _mask_email(session.email),
            "lead_count": lead_count,
            "artifact": str(artifact),
            "delivery_status": delivery_status,
        },
    )
    if lead_count >= 50:
        bucket = "50+"
    elif lead_count >= 25:
        bucket = "25-49"
    elif lead_count > 0:
        bucket = "1-24"
    else:
        bucket = "0"
    metrics.increment(
        "delivery.unlock.completed", tags={"status": delivery_status, "lead_count_bucket": bucket}
    )


def _maybe_email_unlock(recipient: str, body_lines: list[str]) -> str:
    """Send unlock email via SMTP if configured; skip silently if not."""
    if not settings.email_smtp_url or not settings.email_from:
        logger.info("delivery.unlock.email_skipped", extra={"reason": "smtp_not_configured"})
        metrics.increment("delivery.unlock.email_skipped", tags={"reason": "smtp_not_configured"})
        return "skipped"
    logger.info("delivery.unlock.email_attempt", extra={"email_domain": _mask_email(recipient)})
    parsed = urlparse(settings.email_smtp_url)
    if parsed.scheme not in {"smtp", "smtps", "smtp+ssl"}:
        logger.warning("delivery.unlock.email_invalid_scheme", extra={"scheme": parsed.scheme})
        metrics.increment("delivery.unlock.email_failed", tags={"reason": "invalid_scheme"})
        metrics.alert(
            "delivery.unlock.email_failed",
            value=1.0,
            threshold=0.0,
            severity="warning",
            tags={"reason": "invalid_scheme"},
        )
        return "failed"
    host = parsed.hostname
    if not host:
        logger.warning("delivery.unlock.email_missing_host")
        metrics.increment("delivery.unlock.email_failed", tags={"reason": "missing_host"})
        metrics.alert(
            "delivery.unlock.email_failed",
            value=1.0,
            threshold=0.0,
            severity="warning",
            tags={"reason": "missing_host"},
        )
        return "failed"
    port = parsed.port or (465 if parsed.scheme in {"smtps", "smtp+ssl"} else 587)
    user = parsed.username or ""
    password = parsed.password or ""
    use_ssl = parsed.scheme in {"smtps", "smtp+ssl"}
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = recipient
    msg["Subject"] = "Your FundSignal unlock report"
    msg.set_content("\n".join(body_lines))
    try:
        client = (
            smtplib.SMTP_SSL(host, port, timeout=15)
            if use_ssl
            else smtplib.SMTP(host, port, timeout=15)
        )
        try:
            if not use_ssl and not settings.email_disable_tls:
                client.starttls()
            if user or password:
                client.login(user, password)
            client.send_message(msg)
            logger.info(
                "delivery.unlock.email_sent",
                extra={
                    "email_domain": _mask_email(recipient),
                    "message_id": msg["Message-ID"],
                },
            )
            metrics.increment(
                "delivery.unlock.email_sent",
                tags={"protocol": "ssl" if use_ssl else "starttls"},
            )
            return "sent"
        finally:  # noqa: SIM105 - nested try/finally intentional for quit logging
            try:
                client.quit()
            except Exception:  # pragma: no cover - best effort
                logger.debug(
                    "delivery.unlock.email_quit_failed",
                    exc_info=True,
                    extra={"email_domain": _mask_email(recipient)},
                )
    except Exception:
        logger.exception(
            "delivery.unlock.email_failed",
            extra={"email_domain": _mask_email(recipient)},
        )
        metrics.increment("delivery.unlock.email_failed", tags={"reason": "smtp_error"})
        metrics.alert(
            "delivery.unlock.email_failed",
            value=1.0,
            threshold=0.0,
            severity="critical",
            tags={"reason": "smtp_error"},
        )
        return "failed"
    return "sent"


@router.get("/auth/google/url", response_model=GoogleAuthUrlResponse)
async def google_auth_url(plan_id: Optional[str] = None) -> GoogleAuthUrlResponse:
    """Return a Google OAuth consent URL with a signed state token."""
    if not settings.google_client_id or not settings.google_redirect_uri:
        logger.info("auth.google.url.not_enabled")
        raise HTTPException(status_code=503, detail="Google OAuth not configured.")
    validated_plan = _validate_plan(plan_id)
    state = _issue_google_state(validated_plan)
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    url = f"{GOOGLE_AUTH_URL}?{query}"
    logger.info(
        "auth.google.url.issued",
        extra={"state": state, "plan_id": validated_plan},
    )
    return GoogleAuthUrlResponse(url=url, state=state)


@router.post("/auth/opt-out", response_model=OptOutResponse)
async def opt_out(payload: OptOutRequest) -> OptOutResponse:
    """Set or clear email opt-out for unlock deliveries."""
    email_key = payload.email.lower()
    if payload.opt_out:
        _opt_out_emails.add(email_key)
        status = "opted_out"
    else:
        _opt_out_emails.discard(email_key)
        status = "opted_in"
    logger.info(
        "auth.opt_out.updated",
        extra={
            "email_domain": _mask_email(payload.email),
            "opt_out": payload.opt_out,
            "reason": payload.reason,
        },
    )
    return OptOutResponse(status=status, opted_out=payload.opt_out)
