"""
tests/test_v7_returns.py
-------------------------
Tests for the v7.0 Returns / Refund / Exchange (RMA) workflow
(``commerce/returns.py`` and ``admin/returns_routes.py``).

No-network, no-mock tests: the commerce DB is bootstrapped through
:func:`commerce.bootstrap`, a real order is created via ``order_service``, and
the RMA lifecycle is asserted end-to-end (mint -> requested -> approved ->
completed, with the completed refund flipping the order to ``refunded``). The
blueprint is verified by registering it on a throwaway Flask app.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

from decimal import Decimal

import pytest
from flask import Flask

import commerce
from commerce import returns as returns_service
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service
from admin.returns_routes import admin_returns_bp


def _unique_wa() -> str:
    return "9198" + f"{uuid.uuid4().int % 10**8:08d}"


@pytest.fixture(scope="module")
def order() -> dict:
    """Bootstrap the DB and create a single paid order."""
    commerce.bootstrap()
    created = order_service.create_order(
        ParsedOrder(
            wa_number=_unique_wa(),
            customer_name="Return Buyer",
            items=[
                ParsedItem(product_retailer_id="RET-SKU-1", quantity=1,
                           unit_price=Decimal("2000"), product_name="Silk Saree"),
            ],
        )
    )
    order_service.set_payment_status(created["id"], "paid", actor="test")
    return created


# --------------------------------------------------------------------------
# commerce/returns.py
# --------------------------------------------------------------------------

def test_create_return_mints_rma(order):
    rma = returns_service.create_return(
        order["id"], kind="refund", reason="Wrong size", actor="test"
    )
    assert "error" not in rma
    assert rma["rma_number"].startswith("RMA-")
    assert rma["status"] == "requested"
    assert rma["order_id"] == order["id"]
    assert rma["kind"] == "refund"
    assert rma["wa_number"] == order["wa_number"]

    # Retrievable by both numeric id and RMA number.
    assert returns_service.get_return(rma["id"])["rma_number"] == rma["rma_number"]
    assert returns_service.get_return(rma["rma_number"])["id"] == rma["id"]


def test_create_return_unknown_order():
    result = returns_service.create_return(99999999, kind="return")
    assert result.get("error") == "order_not_found"


def test_list_and_count_returns(order):
    returns_service.create_return(order["id"], kind="return", reason="Late", actor="test")
    rows = returns_service.list_returns(limit=50)
    assert isinstance(rows, list) and rows
    requested = returns_service.list_returns(status="requested", limit=50)
    assert all(r["status"] == "requested" for r in requested)
    assert returns_service.count_returns(status="requested") >= 1

    latest = returns_service.latest_return_for(order["wa_number"])
    assert latest is not None and latest["order_id"] == order["id"]


def test_completed_refund_flips_order_to_refunded(order):
    rma = returns_service.create_return(order["id"], kind="refund", actor="test")
    rid = rma["id"]

    approved = returns_service.update_return_status(rid, "approved", actor="test")
    assert approved["status"] == "approved"

    completed = returns_service.update_return_status(
        rid, "completed", refund_amount=2000, resolution="Refund to UPI", actor="test"
    )
    assert completed["status"] == "completed"
    assert completed["refund_amount"] == 2000.0
    assert completed["resolution"] == "Refund to UPI"

    # The linked order is now refunded (status + payment).
    refreshed = order_service.get_order(order_id=order["id"])
    assert refreshed["status"] == "refunded"
    assert refreshed["payment_status"] == "refunded"


def test_update_return_invalid_status(order):
    rma = returns_service.create_return(order["id"], kind="return", actor="test")
    result = returns_service.update_return_status(rma["id"], "bogus")
    assert result.get("error") == "invalid_status"


def test_update_return_missing():
    result = returns_service.update_return_status(99999999, "approved")
    assert result.get("error") == "return_not_found"


# --------------------------------------------------------------------------
# admin/returns_routes.py blueprint wiring
# --------------------------------------------------------------------------

def test_blueprint_registers_expected_routes():
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(admin_returns_bp)

    assert admin_returns_bp.name == "admin_returns"
    rules = {r.rule for r in app.url_map.iter_rules()}
    for expected in {
        "/admin/returns/",
        "/admin/returns/<int:rid>",
        "/admin/returns/<int:rid>/status",
    }:
        assert expected in rules, f"missing route {expected}"

    status_methods = {
        m
        for r in app.url_map.iter_rules()
        if r.rule.endswith("/status")
        for m in r.methods
    }
    assert "POST" in status_methods
