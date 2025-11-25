import logging
from uuid import UUID

from fastapi.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.main import app

client = TestClient(app)


def test_magic_link_flow():
    auth_routes.logger.setLevel(logging.DEBUG)
    resp = client.post("/auth/magic-link", json={"email": "ae@example.com"})
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
