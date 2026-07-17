"""
tests/test_v61_crm.py
----------------------
Tests for the v6.1 Customer CRM (``commerce/crm.py`` and
``admin/crm_routes.py``).

No-network, no-mock tests: the commerce DB is bootstrapped through
:func:`commerce.bootstrap`, a couple of real orders are created for a single
WhatsApp number via ``order_service``, and the pure CRM read/write functions
are asserted. The blueprint is verified by registering it on a throwaway Flask
app and inspecting its URL map.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

from decimal import Decimal

import pytest
from flask import Flask

import commerce
from commerce import crm
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service
from admin.crm_routes import admin_crm_bp

# A run-unique number so repeated test runs against a persistent SQLite file
# stay deterministic for this customer's aggregates (no bleed from prior runs).
WA = "9197" + f"{uuid.uuid4().int % 10**8:08d}"


@pytest.fixture(scope="module")
def seeded() -> dict:
    """Bootstrap the DB and create two orders for one customer."""
    commerce.bootstrap()

    first = order_service.create_order(
        ParsedOrder(
            wa_number=WA,
            customer_name="CRM Buyer",
            items=[
                ParsedItem(product_retailer_id="CRM-SKU-1", quantity=2,
                           unit_price=Decimal("1500"), product_name="Silk Saree"),
            ],
        )
    )
    second = order_service.create_order(
        ParsedOrder(
            wa_number=WA,
            customer_name="CRM Buyer",
            items=[
                ParsedItem(product_retailer_id="CRM-SKU-2", quantity=1,
                           unit_price=Decimal("800"), product_name="Cotton Kurta"),
            ],
        )
    )
    return {"first": first, "second": second}


def _customer_total(seeded: dict) -> float:
    return float(seeded["first"]["total_amount"] + seeded["second"]["total_amount"])


# --------------------------------------------------------------------------
# list_customers / count_customers
# --------------------------------------------------------------------------

def test_list_customers_includes_seeded(seeded):
    rows = crm.list_customers(query=WA, limit=50)
    assert rows, "expected the seeded customer in the list"
    row = next((r for r in rows if r["wa_number"] == WA), None)
    assert row is not None
    assert {"wa_number", "name", "orders_count", "lifetime_value",
            "last_order_at", "segment", "tags"} <= set(row.keys())
    assert row["orders_count"] >= 2
    assert row["lifetime_value"] == pytest.approx(_customer_total(seeded))
    assert isinstance(row["tags"], list)


def test_count_customers(seeded):
    assert crm.count_customers(query=WA) >= 1


# --------------------------------------------------------------------------
# get_customer
# --------------------------------------------------------------------------

def test_get_customer_returns_notes_and_orders(seeded):
    detail = crm.get_customer(WA)
    assert detail is not None
    assert detail["wa_number"] == WA
    assert detail["orders_count"] >= 2
    assert detail["lifetime_value"] == pytest.approx(_customer_total(seeded))
    assert isinstance(detail["notes"], list)
    assert isinstance(detail["orders"], list)
    # Order history is filtered to exactly this customer.
    assert detail["orders"]
    assert all(o["wa_number"] == WA for o in detail["orders"])
    # Average order value is lifetime value / orders count.
    assert detail["avg_order_value"] == pytest.approx(
        detail["lifetime_value"] / detail["orders_count"]
    )


def test_get_customer_unknown_returns_none():
    assert crm.get_customer("910000000000") is None


# --------------------------------------------------------------------------
# add_note
# --------------------------------------------------------------------------

def test_add_note_then_visible(seeded):
    saved = crm.add_note(WA, "Prefers cash on delivery", author="tester")
    assert saved and saved["note"] == "Prefers cash on delivery"

    detail = crm.get_customer(WA)
    notes = [n["note"] for n in detail["notes"]]
    assert "Prefers cash on delivery" in notes
    authored = next(n for n in detail["notes"] if n["note"] == "Prefers cash on delivery")
    assert authored["author"] == "tester"


# --------------------------------------------------------------------------
# set_tags / set_segment
# --------------------------------------------------------------------------

def test_set_tags_then_visible(seeded):
    crm.set_tags(WA, ["vip", "wholesale", "vip"])  # dedupe check
    detail = crm.get_customer(WA)
    assert isinstance(detail["tags"], list)
    assert "vip" in detail["tags"]
    assert "wholesale" in detail["tags"]
    assert detail["tags"].count("vip") == 1


def test_set_segment_then_visible(seeded):
    crm.set_segment(WA, "vip")
    detail = crm.get_customer(WA)
    assert detail["segment"] == "vip"


# --------------------------------------------------------------------------
# suggested segment logic
# --------------------------------------------------------------------------

def test_suggest_segment_logic():
    assert crm.suggest_segment(30000, 1) == "vip"      # by lifetime value
    assert crm.suggest_segment(0, 5) == "vip"          # by order count
    assert crm.suggest_segment(1000, 2) == "repeat"    # repeat buyer
    assert crm.suggest_segment(1000, 1) == "new"       # single order


def test_recompute_profile(seeded):
    profile = crm.recompute_profile(WA)
    assert profile["wa_number"] == WA
    assert profile["orders_count"] >= 2
    assert profile["lifetime_value"] == pytest.approx(_customer_total(seeded))
    assert profile["display_name"] == "CRM Buyer"


# --------------------------------------------------------------------------
# Blueprint wiring
# --------------------------------------------------------------------------

def test_blueprint_registers_expected_routes():
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(admin_crm_bp)

    assert admin_crm_bp.name == "admin_crm"
    rules = {r.rule for r in app.url_map.iter_rules()}
    for expected in {
        "/admin/commerce/crm/",
        "/admin/commerce/crm/<wa_number>",
        "/admin/commerce/crm/<wa_number>/note",
        "/admin/commerce/crm/<wa_number>/tags",
        "/admin/commerce/crm/<wa_number>/segment",
    }:
        assert expected in rules, f"missing route {expected}"

    post_rules = {
        r.rule
        for r in app.url_map.iter_rules()
        if "POST" in r.methods
    }
    assert "/admin/commerce/crm/<wa_number>/note" in post_rules
    assert "/admin/commerce/crm/<wa_number>/tags" in post_rules
    assert "/admin/commerce/crm/<wa_number>/segment" in post_rules
