"""
tests/test_v7_ops.py
---------------------
Tests for the v7.0 Admin/Ops depth:

* ``commerce/settings_store.py`` — set/get roundtrip + ``all_settings``.
* ``commerce/broadcast.py`` — ``recipient_count`` honours marketing consent.
* ``admin/ops_routes.py`` — ``payments_dashboard_data`` returns a dict.
* Blueprint wiring for the settings / ops / broadcast blueprints.

No-network, no-mock: the commerce DB is bootstrapped through
:func:`commerce.bootstrap`; a consenting :class:`CrmProfile` and a real order +
payment are created directly. Broadcasts are not actually delivered (jobs are
disabled in ``conftest``); only recipient resolution is asserted.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

from decimal import Decimal

import pytest
from flask import Flask

import commerce
from commerce import broadcast, settings_store
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service
from admin.ops_routes import admin_ops_bp, payments_dashboard_data, employees_data
from admin.settings_routes import admin_settings_bp
from admin.broadcast_routes import admin_broadcast_bp


def _unique_wa() -> str:
    return "9196" + f"{uuid.uuid4().int % 10**8:08d}"


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    commerce.bootstrap()


# --------------------------------------------------------------------------
# commerce/settings_store.py
# --------------------------------------------------------------------------

def test_settings_set_get_roundtrip():
    key = "delivery_estimate"
    settings_store.set_setting(key, "3-5 business days", actor="test")
    assert settings_store.get_setting(key) == "3-5 business days"

    # Overwrite updates in place.
    settings_store.set_setting(key, "2-4 business days", actor="test")
    assert settings_store.get_setting(key) == "2-4 business days"

    # Unknown key falls back to the provided default.
    assert settings_store.get_setting("does_not_exist", "fallback") == "fallback"


def test_all_settings_contains_written_key():
    settings_store.set_setting("business_name", "ME-HAAT Fashion", actor="test")
    everything = settings_store.all_settings()
    assert isinstance(everything, dict)
    assert everything.get("business_name") == "ME-HAAT Fashion"


def test_editable_settings_shape():
    assert settings_store.EDITABLE_SETTINGS
    keys = {item["key"] for item in settings_store.EDITABLE_SETTINGS}
    for expected in {
        "business_name", "business_gstin", "delivery_estimate",
        "low_stock_threshold", "coupons_enabled", "auto_draft_order",
        "payment_provider", "shipping_provider", "abandoned_cart_hours",
    }:
        assert expected in keys
    for item in settings_store.EDITABLE_SETTINGS:
        assert {"key", "label", "type", "help"} <= set(item)


# --------------------------------------------------------------------------
# commerce/broadcast.py
# --------------------------------------------------------------------------

def _make_crm_profile(*, consent: bool, segment=None, tags="") -> str:
    from database.db import session_scope
    from database.models import CrmProfile

    wa = _unique_wa()
    with session_scope() as session:
        session.add(CrmProfile(
            wa_number=wa, marketing_consent=consent, segment=segment, tags=tags,
        ))
    return wa


def test_recipient_count_respects_consent():
    consenting = _make_crm_profile(consent=True, segment="vip", tags="diwali")
    _make_crm_profile(consent=False, segment="vip", tags="diwali")

    # consent_only (default) counts only the consenting profile in this segment.
    consented = broadcast.recipient_count(segment="vip", consent_only=True)
    everyone = broadcast.recipient_count(segment="vip", consent_only=False)
    assert consented >= 1
    assert everyone >= consented + 1 or everyone > consented

    # Tag filter narrows further and still finds the consenting profile.
    by_tag = broadcast.recipient_count(tag="diwali", consent_only=True)
    assert by_tag >= 1
    assert consenting  # created


def test_send_broadcast_empty_message():
    result = broadcast.send_broadcast("   ", consent_only=True)
    assert result["ok"] is False
    assert result["recipients"] == 0


def test_send_broadcast_returns_recipient_count():
    _make_crm_profile(consent=True, segment="broadcast_seg")
    result = broadcast.send_broadcast(
        "Hello from ME-HAAT!", segment="broadcast_seg", consent_only=True, actor="test"
    )
    assert result["ok"] is True
    assert result["recipients"] >= 1


def test_register_broadcast_handler():
    # Registers without raising and wires the handler into the job registry.
    broadcast.register_broadcast_handler()
    from commerce.jobs import _get_handler

    assert _get_handler(broadcast.BROADCAST_JOB_KIND) is not None


# --------------------------------------------------------------------------
# admin/ops_routes.py data functions
# --------------------------------------------------------------------------

def test_payments_dashboard_data_returns_dict():
    # Seed a paid payment so the aggregates are exercised.
    order = order_service.create_order(
        ParsedOrder(
            wa_number=_unique_wa(),
            customer_name="Pay Buyer",
            items=[ParsedItem(product_retailer_id="RET-PAY-1", quantity=1,
                              unit_price=Decimal("1500"), product_name="Kurta")],
        )
    )
    order_service.record_payment(
        order["id"], provider="razorpay", amount=Decimal("1500"),
        currency="INR", status="paid",
    )

    data = payments_dashboard_data()
    assert isinstance(data, dict)
    for key in ("payments", "total_collected", "total_pending", "count", "by_provider"):
        assert key in data
    assert isinstance(data["payments"], list)
    assert isinstance(data["by_provider"], list)
    assert data["total_collected"] >= 1500.0


def test_employees_data_returns_list():
    rows = employees_data()
    assert isinstance(rows, list)


# --------------------------------------------------------------------------
# Blueprint wiring
# --------------------------------------------------------------------------

def test_blueprints_register_expected_routes():
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(admin_settings_bp)
    app.register_blueprint(admin_ops_bp)
    app.register_blueprint(admin_broadcast_bp)

    assert admin_settings_bp.name == "admin_settings"
    assert admin_ops_bp.name == "admin_ops"
    assert admin_broadcast_bp.name == "admin_broadcast"

    rules = {r.rule for r in app.url_map.iter_rules()}
    for expected in {
        "/admin/settings/",
        "/admin/ops/payments",
        "/admin/ops/payments/data",
        "/admin/ops/employees",
        "/admin/broadcast/",
    }:
        assert expected in rules, f"missing route {expected}"
