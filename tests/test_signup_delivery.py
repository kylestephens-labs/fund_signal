import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

import app.api.routes.delivery as delivery_routes
from app.api.routes import auth as auth_routes
from app.config import settings
from app.main import app
from app.models.subscription import ProcessedEvent, Subscription

client = TestClient(app)
_SYNC_SESSION_FACTORY: sessionmaker[Session] | None = None


@pytest.fixture(autouse=True)
def reset_auth_state(tmp_path, monkeypatch):
    auth_routes._tokens.clear()
    auth_routes._otp_codes.clear()
    auth_routes._google_states.clear()
    auth_routes._sessions.clear()
    auth_routes._opt_out_emails.clear()
    auth_routes._unlock_sent.clear()
    auth_routes._rate_limiter.reset()
    auth_routes.logger.setLevel(logging.INFO)
    settings.delivery_output_dir = str(tmp_path)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_stub")
    settings.stripe_secret_key = "sk_test_stub"  # noqa: S105 - test fixture value
    yield


@pytest.fixture(autouse=True)
def override_db():
    """Provide a real in-memory SQLite DB for tests without JSON fakes."""

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    sync_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    class SyncAsyncSession:
        def __init__(self):
            self._session = sync_factory()

        async def execute(self, stmt):
            return self._session.execute(stmt)

        async def commit(self):
            self._session.commit()

        async def rollback(self):
            self._session.rollback()

        def add(self, obj):
            self._session.add(obj)

    async def override():
        session = SyncAsyncSession()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            session._session.close()

    app.dependency_overrides[delivery_routes.get_database] = override
    global _SYNC_SESSION_FACTORY
    _SYNC_SESSION_FACTORY = sync_factory
    yield sync_factory
    app.dependency_overrides.pop(delivery_routes.get_database, None)
    engine.dispose()


def _auth_headers(email: str) -> dict[str, str]:
    auth_routes.logger.setLevel(logging.DEBUG)
    auth_routes._rate_limiter.max_requests = 10
    resp_issue = client.post("/auth/magic-link", json={"email": email})
    token = resp_issue.json()["debug_token"]
    verified = client.post("/auth/magic-link/verify", json={"token": token})
    session_token = verified.json()["session_token"]
    return {"Authorization": f"Bearer {session_token}"}


def _sign_payload(secret: str, payload: str, timestamp: str) -> str:
    signature = hmac.new(
        secret.encode(),
        msg=f"{timestamp}.{payload}".encode(),
        digestmod=sha256,
    ).hexdigest()
    return f"t={timestamp},v1={signature}"


class _FakePaymentMethod:
    def __init__(self, pm_id: str, customer: str):
        self.id = pm_id
        self.customer = customer


class FakeStripe:
    def __init__(self):
        self.customers: dict[str, dict[str, str]] = {}
        self.subscriptions: dict[str, dict[str, str | int | bool]] = {}
        self.payment_methods: dict[str, _FakePaymentMethod] = {}
        self.Customer = type(
            "Customer",
            (),
            {
                "list": self._customer_list,
                "create": self._customer_create,
                "modify": self._customer_modify,
            },
        )
        self.PaymentMethod = type(
            "PaymentMethod",
            (),
            {"attach": self._payment_method_attach},
        )
        self.Subscription = type(
            "Subscription",
            (),
            {
                "create": self._subscription_create,
                "modify": self._subscription_modify,
            },
        )
        self.Webhook = type(
            "Webhook",
            (),
            {"construct_event": self._construct_event},
        )

    class error:  # noqa: N801 - mimic Stripe SDK structure
        class StripeError(Exception):
            pass

    def _customer_list(self, email: str, limit: int):
        return type(
            "ListObj",
            (),
            {"data": [c for c in self.customers.values() if c["email"] == email][:limit]},
        )

    def _customer_create(self, email: str):
        cid = f"cus_{len(self.customers)+1}"
        customer = {"id": cid, "email": email}
        self.customers[cid] = customer
        return customer

    def _customer_modify(self, customer_id: str, invoice_settings: dict[str, str]):
        customer = self.customers.get(customer_id)
        if customer:
            customer["invoice_settings"] = invoice_settings
        return customer

    def _payment_method_attach(self, pm_id: str, customer: str):
        self.payment_methods[pm_id] = _FakePaymentMethod(pm_id, customer)
        return {"id": pm_id, "customer": customer}

    def _subscription_create(
        self,
        customer: str,
        items: list[dict[str, str]],
        trial_period_days: int,
        payment_behavior: str,
        payment_settings: dict[str, str],
        default_payment_method: str,
        expand: list[str],
    ):
        sub_id = f"sub_{len(self.subscriptions)+1}"
        now = int(datetime.now(tz=timezone.utc).timestamp())  # noqa: UP017
        trial_end = now + trial_period_days * 24 * 3600
        sub = {
            "id": sub_id,
            "customer": customer,
            "status": "trialing",
            "trial_start": now,
            "trial_end": trial_end,
            "current_period_end": trial_end,
            "cancel_at_period_end": False,
            "plan": {"id": items[0]["price"]},
            "default_payment_method": default_payment_method,
            "latest_invoice": {
                "payment_intent": {
                    "id": f"pi_{sub_id}",
                    "client_secret": f"pi_secret_{sub_id}",
                }
            },
        }
        self.subscriptions[sub_id] = sub
        return sub

    def _subscription_modify(self, sub_id: str, cancel_at_period_end: bool):
        sub = self.subscriptions.get(sub_id)
        if sub:
            sub["cancel_at_period_end"] = cancel_at_period_end
            sub["status"] = "canceled"
        return sub

    def _construct_event(self, payload: bytes, sig_header: str | None, secret: str | None):
        return json.loads(payload.decode())


@pytest.fixture(autouse=True)
def fake_stripe(monkeypatch):
    client = FakeStripe()
    monkeypatch.setattr(delivery_routes, "stripe", client)
    return client


def test_magic_link_flow():
    auth_routes.logger.setLevel(logging.DEBUG)
    auth_routes._rate_limiter.max_requests = 10
    resp = client.post("/auth/magic-link", json={"email": "ae@example.com", "plan_id": "starter"})
    assert resp.status_code == 202
    payload = resp.json()
    token = payload.get("debug_token")
    assert payload["expires_in"] == auth_routes.MAGIC_LINK_TTL_SECONDS
    assert token, "expected debug token when logger set to DEBUG"

    verify = client.post("/auth/magic-link/verify", json={"token": token})
    assert verify.status_code == 200
    data = verify.json()
    assert data["status"] == "verified"
    assert data["subscription"]["status"] == "trialing"
    assert data["subscription"]["plan_id"] == "starter"
    assert data["session_token"]
    artifacts = list(Path(settings.delivery_output_dir).glob("unlock_email_*.md"))
    assert artifacts, "expected unlock email artifact written"


def test_otp_flow_with_plan_and_email_match():
    auth_routes.logger.setLevel(logging.DEBUG)
    auth_routes._rate_limiter.max_requests = 10
    resp = client.post("/auth/otp", json={"email": "bd@example.com", "plan_id": "pro"})
    assert resp.status_code == 202
    otp = resp.json()["debug_token"]

    verify = client.post("/auth/otp/verify", json={"email": "bd@example.com", "otp": otp})
    assert verify.status_code == 200
    data = verify.json()
    assert data["subscription"]["plan_id"] == "pro"
    assert data["session_token"]


def test_otp_email_mismatch_rejected():
    auth_routes.logger.setLevel(logging.DEBUG)
    resp = client.post("/auth/otp", json={"email": "wrong@example.com"})
    otp = resp.json()["debug_token"]

    verify = client.post("/auth/otp/verify", json={"email": "other@example.com", "otp": otp})
    assert verify.status_code == 400


def test_invalid_plan_rejected():
    resp = client.post(
        "/auth/magic-link", json={"email": "ae@example.com", "plan_id": "enterprise"}
    )
    assert resp.status_code == 400
    assert "plan" in resp.json()["detail"].lower()


def test_auth_rate_limit_enforced(monkeypatch):
    auth_routes.logger.setLevel(logging.INFO)
    # constrain limiter for test
    auth_routes._rate_limiter.max_requests = 1
    resp1 = client.post("/auth/magic-link", json={"email": "rl@example.com"})
    assert resp1.status_code == 202
    resp2 = client.post("/auth/magic-link", json={"email": "rl@example.com"})
    assert resp2.status_code == 429
    assert resp2.headers.get("Retry-After") is not None
    # allow window to expire
    time.sleep(0.1)
    auth_routes._rate_limiter.reset()


def test_google_oauth_flow_success(monkeypatch):
    settings.google_client_id = "client"
    settings.google_client_secret = "secret"  # noqa: S105 - test fixture value
    settings.google_redirect_uri = "http://localhost:8000/auth/google/callback"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, data=None, **kwargs):
            assert url == auth_routes.GOOGLE_TOKEN_URL
            assert data["code"] == "code"
            return httpx.Response(
                200,
                json={"access_token": "token"},
                request=httpx.Request("POST", url),
            )

        async def get(self, url, headers=None, **kwargs):
            assert url == auth_routes.GOOGLE_USERINFO_URL
            assert headers and "Bearer token" in headers.get("Authorization", "")
            return httpx.Response(
                200,
                json={"email": "google.user@example.com"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(auth_routes.httpx, "AsyncClient", FakeAsyncClient)

    url_resp = client.get("/auth/google/url", params={"plan_id": "starter"})
    assert url_resp.status_code == 200
    payload = url_resp.json()
    assert "state" in payload and payload["state"]
    state = payload["state"]

    cb_resp = client.post("/auth/google/callback", json={"code": "code", "state": state})
    assert cb_resp.status_code == 200
    data = cb_resp.json()
    assert data["status"] == "verified"
    assert data["email"] == "google.user@example.com"
    assert data["subscription"]["status"] == "trialing"
    assert data["session_token"]


def test_google_oauth_missing_code():
    settings.google_client_id = "client"
    settings.google_client_secret = "secret"  # noqa: S105 - test fixture value
    settings.google_redirect_uri = "http://localhost:8000/auth/google/callback"
    resp = client.post("/auth/google/callback", json={"state": "state"})
    assert resp.status_code == 400


def test_google_oauth_expired_state():
    settings.google_client_id = "client"
    settings.google_client_secret = "secret"  # noqa: S105 - test fixture value
    settings.google_redirect_uri = "http://localhost:8000/auth/google/callback"
    state = "expired"
    auth_routes._google_states[state] = auth_routes._GoogleState(
        plan_id=None,
        expires_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1),  # noqa: UP017
    )
    resp = client.post("/auth/google/callback", json={"code": "code", "state": state})
    assert resp.status_code == 400


def test_leads_endpoint_filters_and_limits():
    auth_routes.logger.setLevel(logging.DEBUG)
    resp_issue = client.post("/auth/magic-link", json={"email": "leadtest@example.com"})
    token = resp_issue.json()["debug_token"]
    verified = client.post("/auth/magic-link/verify", json={"token": token})
    session_token = verified.json()["session_token"]
    resp = client.get(
        "/leads",
        params={"score_gte": 40, "limit": 2},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 200
    leads = resp.json()
    assert len(leads) <= 2
    for lead in leads:
        assert int(lead["score"]) >= 40
        # UUID validation
        UUID(lead["company_id"])
        assert "freshness" in lead
        assert "report_generated_at" in lead
        assert "upgrade_cta" in lead


def test_delivery_stub_paths_written(tmp_path, monkeypatch):
    from app.config import settings

    settings.delivery_output_dir = str(tmp_path)
    resp = client.post("/delivery/weekly", json={"force": False})
    assert resp.status_code == 202
    body = resp.json()
    assert body["queued"] is True
    for path in body["output_paths"]:
        assert str(tmp_path) in path


def test_leads_requires_auth():
    resp = client.get("/leads")
    assert resp.status_code == 401


def test_opt_out_blocks_unlock_email(tmp_path):
    settings.delivery_output_dir = str(tmp_path)
    auth_routes.logger.setLevel(logging.DEBUG)
    issue = client.post("/auth/magic-link", json={"email": "optout@example.com"})
    token = issue.json()["debug_token"]
    client.post("/auth/opt-out", json={"email": "optout@example.com", "opt_out": True})
    verify = client.post("/auth/magic-link/verify", json={"token": token})
    assert verify.status_code == 200
    artifacts = list(Path(settings.delivery_output_dir).glob("unlock_email_*.md"))
    assert artifacts == [], "no unlock email should be written when opted out"


def test_subscribe_creates_trial_and_dates():
    headers = _auth_headers("trial@example.com")
    resp = client.post(
        "/billing/subscribe",
        json={
            "plan_id": "starter",
            "payment_method_id": "pm_test",
            "customer_email": "trial@example.com",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "trialing"
    assert data["plan_id"] == "starter"
    trial_start = datetime.fromisoformat(data["trial_start"])
    trial_end = datetime.fromisoformat(data["trial_end"])
    assert (trial_end - trial_start).days == 14
    assert data["payment_behavior"] == "default_incomplete"
    assert data["client_secret"] == f"pi_secret_{data['subscription_id']}"
    subscription = _get_subscription(data["subscription_id"])
    assert subscription is not None


def test_cancel_sets_cancel_at_period_end():
    headers = _auth_headers("cancelme@example.com")
    sub = client.post(
        "/billing/subscribe",
        json={
            "price_id": "price_123",
            "payment_method_id": "pm_cancel",
            "customer_email": "cancelme@example.com",
        },
        headers=headers,
    ).json()
    cancel = client.post(
        "/billing/cancel",
        json={"subscription_id": sub["subscription_id"], "reason": "testing"},
        headers=headers,
    )
    assert cancel.status_code == 200
    body = cancel.json()
    assert body["status"] == "cancelled"
    assert body["effective_date"]
    stored = _get_subscription(sub["subscription_id"])
    assert stored is not None
    assert stored.cancel_at_period_end is True
    assert stored.status in {"cancelled", "canceled"}


def test_subscribe_requires_payment_method():
    headers = _auth_headers("nopm@example.com")
    resp = client.post(
        "/billing/subscribe",
        json={"plan_id": "starter", "customer_email": "nopm@example.com"},
        headers=headers,
    )
    assert resp.status_code == 400


def test_stripe_webhook_idempotent_and_signature():
    settings.stripe_webhook_secret = "whsec_test"  # noqa: S105 - test fixture value
    event = {
        "id": "evt_123",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_abc",
                "status": "trialing",
                "trial_start": 1,
                "trial_end": 2,
                "current_period_end": 2,
                "cancel_at_period_end": False,
                "customer_email": "webhook@example.com",
                "customer": "cus_abc",
                "plan": {"id": "price_abc"},
            }
        },
    }
    payload = json.dumps(event)
    timestamp = "123456789"
    signature = _sign_payload(settings.stripe_webhook_secret, payload, timestamp)
    first = client.post(
        "/billing/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    second = client.post(
        "/billing/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    subscription = _get_subscription("sub_abc")
    assert subscription is not None and subscription.last_event_id == "evt_123"
    assert _get_processed_event("evt_123") is not None


def test_checkout_session_completed_persists_subscription():
    settings.stripe_webhook_secret = "whsec_test"  # noqa: S105 - test fixture value
    _seed_subscription(
        sub_id="sub_checkout",
        price_id="price_checkout",
        customer_id="cus_checkout",
        email="checkout@example.com",
        status="trialing",
    )
    event = {
        "id": "evt_checkout",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "subscription": "sub_checkout",
                "customer": "cus_checkout",
                "customer_email": "checkout@example.com",
                "price": {"id": "price_checkout"},
            }
        },
    }
    payload = json.dumps(event)
    timestamp = "123456789"
    signature = _sign_payload(settings.stripe_webhook_secret, payload, timestamp)
    first = client.post(
        "/billing/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    subscription = _get_subscription("sub_checkout")
    assert subscription is not None
    assert subscription.customer_id == "cus_checkout"
    assert subscription.email == "checkout@example.com"
    second = client.post(
        "/billing/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert _get_processed_event("evt_checkout") is not None


def test_trial_will_end_logged_and_deduped():
    settings.stripe_webhook_secret = "whsec_test"  # noqa: S105 - test fixture value
    event = {
        "id": "evt_trial",
        "type": "customer.subscription.trial_will_end",
        "data": {
            "object": {
                "id": "sub_trial",
                "status": "trialing",
                "trial_end": 2,
                "trial_start": 1,
                "current_period_end": 2,
                "customer_email": "trial@example.com",
                "customer": "cus_trial",
                "plan": {"id": "price_trial"},
            }
        },
    }
    payload = json.dumps(event)
    timestamp = "123456789"
    signature = _sign_payload(settings.stripe_webhook_secret, payload, timestamp)
    first = client.post(
        "/billing/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    subscription = _get_subscription("sub_trial")
    assert subscription is not None
    assert subscription.status == "trialing"
    second = client.post(
        "/billing/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True


def _seed_subscription(
    sub_id: str, price_id: str, customer_id: str, email: str, status: str = "trialing"
) -> None:
    if not _SYNC_SESSION_FACTORY:
        return
    with _SYNC_SESSION_FACTORY() as session:
        existing = session.execute(
            select(Subscription).where(Subscription.subscription_id == sub_id)
        ).scalar_one_or_none()
        if existing:
            return
        session.add(
            Subscription(
                subscription_id=sub_id,
                customer_id=customer_id,
                email=email,
                price_id=price_id,
                status=status,
                trial_start=datetime.now(timezone.utc),  # noqa: UP017
                trial_end=datetime.now(timezone.utc) + timedelta(days=14),  # noqa: UP017
                current_period_end=datetime.now(timezone.utc) + timedelta(days=14),  # noqa: UP017
            )
        )
        session.commit()


def _get_subscription(sub_id: str) -> Subscription | None:
    if not _SYNC_SESSION_FACTORY:
        return None
    with _SYNC_SESSION_FACTORY() as session:
        result = session.execute(select(Subscription).where(Subscription.subscription_id == sub_id))
        return result.scalar_one_or_none()


def _get_processed_event(event_id: str) -> ProcessedEvent | None:
    if not _SYNC_SESSION_FACTORY:
        return None
    with _SYNC_SESSION_FACTORY() as session:
        result = session.execute(select(ProcessedEvent).where(ProcessedEvent.event_id == event_id))
        return result.scalar_one_or_none()
