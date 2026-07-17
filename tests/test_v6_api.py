"""
tests/test_v6_api.py
---------------------
Tests for the v6.0 JSON order/tracking API + payment-webhook blueprint
(``commerce/api.py`` + ``commerce/auth.py``).

The blueprint is not yet registered on the real app, so each test registers
``commerce_api_bp`` onto a throwaway :class:`flask.Flask` instance and drives it
via the test client. Persistence uses the default SQLite database bootstrapped
through :func:`commerce.bootstrap`; a single order is created up-front so the
read/tracking endpoints have data.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

import commerce
import commerce.api as api
import commerce.auth as auth
from commerce.api import commerce_api_bp
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service


@pytest.fixture(scope="module")
def seed_order() -> dict:
    """Bootstrap the commerce DB and create one order with tracking history."""
    commerce.bootstrap()
    parsed = ParsedOrder(
        wa_number="919812345678",
        customer_name="Test Buyer",
        items=[ParsedItem(product_retailer_id="SKU-API-1", quantity=2)],
    )
    order = order_service.create_order(parsed)
    # Advance the status so a tracking event exists.
    order_service.set_status(order["id"], "confirmed", actor="test")
    return order


@pytest.fixture
def client() -> "Flask.test_client":
    """Return a test client for a fresh app hosting the commerce blueprint."""
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(commerce_api_bp)
    return app.test_client()


def test_tracking_public_returns_stages(client, seed_order):
    """Public tracking returns 200, the right order_number, and stages list."""
    resp = client.get(f"/tracking/{seed_order['order_number']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["order_number"] == seed_order["order_number"]
    assert isinstance(data["stages"], list)
    stage_names = [s["stage"] for s in data["stages"]]
    assert stage_names == [
        "received",
        "confirmed",
        "packed",
        "shipped",
        "out_for_delivery",
        "delivered",
    ]
    # "confirmed" is the live status -> current; "received" precedes it -> done.
    by_name = {s["stage"]: s["state"] for s in data["stages"]}
    assert by_name["confirmed"] == "current"
    assert by_name["received"] == "done"
    assert by_name["delivered"] == "pending"


def test_orders_requires_auth(client, seed_order):
    """GET /orders without any credential returns 401."""
    resp = client.get("/orders")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "unauthorized"}


def test_token_then_orders_with_bearer(client, seed_order, monkeypatch):
    """A valid /api/token grants a bearer usable on GET /orders."""
    test_config = SimpleNamespace(jwt_secret="testsecret", jwt_expiry_minutes=60)
    # issue_token / decode_token / require_api_auth all read commerce.auth.config.
    monkeypatch.setattr(auth, "config", test_config)
    # Admin credential verification is stubbed to accept the login.
    monkeypatch.setattr(api, "verify_username", lambda u: True)
    monkeypatch.setattr(api, "verify_password", lambda p: True)

    token_resp = client.post("/api/token", json={"username": "admin", "password": "pw"})
    assert token_resp.status_code == 200
    token_body = token_resp.get_json()
    token = token_body["token"]
    assert token
    assert token_body["expires_in"] == 60 * 60

    orders_resp = client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert orders_resp.status_code == 200
    orders_body = orders_resp.get_json()
    assert orders_body["count"] >= 1
    assert isinstance(orders_body["results"], list)


def test_payments_webhook_always_acks(client, monkeypatch):
    """The webhook endpoint returns 200 and echoes ok from handle_webhook."""
    monkeypatch.setattr(
        "payments.handle_webhook",
        lambda provider, headers, raw: {"ok": True, "status": "", "order": None},
    )
    resp = client.post("/payments/webhook/manual_upi", data=b"{}")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
