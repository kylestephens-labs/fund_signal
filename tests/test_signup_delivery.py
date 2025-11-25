import logging
import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_auth_state():
    auth_routes._tokens.clear()
    auth_routes._otp_codes.clear()
    auth_routes._rate_limiter.reset()
    auth_routes.logger.setLevel(logging.INFO)
    yield


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


def test_leads_endpoint_filters_and_limits():
    resp = client.get("/leads", params={"score_gte": 40, "limit": 2})
    assert resp.status_code == 200
    leads = resp.json()
    assert len(leads) <= 2
    for lead in leads:
        assert int(lead["score"]) >= 40
        # UUID validation
        UUID(lead["company_id"])


def test_stripe_webhook_idempotent():
    event = {"id": "evt_123", "type": "customer.subscription.created", "data": {}}
    first = client.post("/billing/stripe/webhook", json=event)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False

    second = client.post("/billing/stripe/webhook", json=event)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True


def test_delivery_stub_paths_written(tmp_path, monkeypatch):
    from app.config import settings

    settings.delivery_output_dir = str(tmp_path)
    resp = client.post("/delivery/weekly", json={"force": False})
    assert resp.status_code == 202
    body = resp.json()
    assert body["queued"] is True
    for path in body["output_paths"]:
        assert str(tmp_path) in path
