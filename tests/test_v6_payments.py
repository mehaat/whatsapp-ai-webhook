"""
tests/test_v6_payments.py
--------------------------
Unit tests for the v6.0 ``payments`` package. Pure logic — NO network.

Covered:
    - ManualUpiProvider builds a correct ``upi://`` deep link.
    - factory.get_provider resolves manual_upi and unknown names to ManualUpi.
    - Razorpay webhook signature verification (valid + forged) and status map.

The real ``config`` is a frozen dataclass, so tests swap the whole ``config``
reference on the module under test via ``monkeypatch.setattr`` with a
``types.SimpleNamespace`` shim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import types
from urllib.parse import parse_qs, urlparse

import payments.factory as factory
import payments.manual_upi as manual_upi_module
import payments.razorpay_provider as razorpay_module
from payments.manual_upi import ManualUpiProvider
from payments.razorpay_provider import RazorpayProvider


# --------------------------------------------------------------------------
# Manual UPI deep link
# --------------------------------------------------------------------------

def test_manual_upi_builds_deep_link(monkeypatch):
    monkeypatch.setattr(
        manual_upi_module,
        "config",
        types.SimpleNamespace(
            upi_vpa="test@upi",
            upi_payee_name="ME-HAAT",
            business_name="ME-HAAT Fashion",
            shopify_app_url="https://x",
            payment_link_expiry_minutes=60,
            default_currency="INR",
        ),
    )

    order = {
        "id": 7,
        "order_number": "MH-0007",
        "currency": "INR",
        "total_amount": 1499.5,
        "wa_number": "919812345678",
        "customer_name": "Asha",
    }
    link = ManualUpiProvider().create_link(order)

    assert link.url.startswith("upi://pay?")
    parsed = urlparse(link.url)
    params = parse_qs(parsed.query)
    assert params["pa"] == ["test@upi"]
    assert params["pn"] == ["ME-HAAT"]
    assert params["cu"] == ["INR"]
    assert params["am"] == ["1499.50"]
    assert params["tn"] == ["MH-0007"]
    assert link.provider == "manual_upi"
    assert link.expires_at is not None


def test_manual_upi_fallback_when_no_vpa(monkeypatch):
    monkeypatch.setattr(
        manual_upi_module,
        "config",
        types.SimpleNamespace(
            upi_vpa="",
            upi_payee_name="ME-HAAT",
            business_name="ME-HAAT Fashion",
            shopify_app_url="https://shop.example.com",
            payment_link_expiry_minutes=60,
            default_currency="INR",
        ),
    )
    order = {"id": 3, "order_number": "MH-0003", "currency": "INR", "total_amount": 100}
    link = ManualUpiProvider().create_link(order)
    assert link.url == "https://shop.example.com/pay/MH-0003"


def test_manual_upi_webhook_unsupported():
    result = ManualUpiProvider().verify_and_parse_webhook({}, b"{}")
    assert result.ok is False
    assert result.event == "unsupported"


# --------------------------------------------------------------------------
# Factory selection
# --------------------------------------------------------------------------

def test_factory_returns_manual_for_manual_upi(monkeypatch):
    monkeypatch.setattr(
        factory, "config", types.SimpleNamespace(payment_provider="manual_upi")
    )
    provider = factory.get_provider("manual_upi")
    assert isinstance(provider, ManualUpiProvider)


def test_factory_returns_manual_for_unknown(monkeypatch):
    monkeypatch.setattr(
        factory, "config", types.SimpleNamespace(payment_provider="manual_upi")
    )
    provider = factory.get_provider("does_not_exist")
    assert isinstance(provider, ManualUpiProvider)


def test_factory_returns_razorpay_for_razorpay(monkeypatch):
    monkeypatch.setattr(
        factory, "config", types.SimpleNamespace(payment_provider="manual_upi")
    )
    provider = factory.get_provider("razorpay")
    assert isinstance(provider, RazorpayProvider)


# --------------------------------------------------------------------------
# Razorpay webhook signature verification
# --------------------------------------------------------------------------

_SECRET = "whsec_test_razorpay"


def _razorpay_body(status: str = "paid") -> bytes:
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_123",
                    "status": status,
                    "reference_id": "MH-0007",
                    "notes": {"order_id": "7", "order_number": "MH-0007"},
                }
            },
            "payment": {"entity": {"id": "pay_abc", "status": "captured"}},
        },
    }
    return json.dumps(payload).encode("utf-8")


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_razorpay_webhook_valid_signature(monkeypatch):
    monkeypatch.setattr(
        razorpay_module,
        "config",
        types.SimpleNamespace(razorpay_webhook_secret=_SECRET),
    )
    body = _razorpay_body("paid")
    headers = {"X-Razorpay-Signature": _sign(_SECRET, body)}

    result = RazorpayProvider().verify_and_parse_webhook(headers, body)
    assert result.ok is True
    assert result.status == "paid"
    assert result.provider_link_id == "plink_123"
    assert result.provider_payment_id == "pay_abc"
    assert result.order_id == 7


def test_razorpay_webhook_forged_signature(monkeypatch):
    monkeypatch.setattr(
        razorpay_module,
        "config",
        types.SimpleNamespace(razorpay_webhook_secret=_SECRET),
    )
    body = _razorpay_body("paid")
    headers = {"X-Razorpay-Signature": "deadbeef"}

    result = RazorpayProvider().verify_and_parse_webhook(headers, body)
    assert result.ok is False
    assert result.status == ""


def test_razorpay_webhook_failed_status_maps(monkeypatch):
    monkeypatch.setattr(
        razorpay_module,
        "config",
        types.SimpleNamespace(razorpay_webhook_secret=_SECRET),
    )
    body = _razorpay_body("cancelled")
    headers = {"X-Razorpay-Signature": _sign(_SECRET, body)}
    result = RazorpayProvider().verify_and_parse_webhook(headers, body)
    assert result.ok is True
    assert result.status == "failed"
