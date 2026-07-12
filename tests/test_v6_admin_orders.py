"""
tests/test_v6_admin_orders.py
------------------------------
Tests for the v6.0 Admin Orders dashboard + order analytics
(``admin/orders_routes.py`` and ``commerce/analytics.py``).

These are no-network, no-mock-required tests: the commerce DB is bootstrapped
through :func:`commerce.bootstrap`, a couple of real orders are created via
``order_service``, and the pure-read analytics functions are asserted to return
well-formed structures. The blueprint itself is verified by registering it on a
throwaway Flask app and inspecting its URL map (avoiding a live login session).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from flask import Flask

import commerce
from commerce import analytics
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service
from admin.orders_routes import admin_orders_bp


@pytest.fixture(scope="module")
def seeded() -> dict:
    """Bootstrap the DB and create a paid + a delivered order with items."""
    commerce.bootstrap()

    paid = order_service.create_order(
        ParsedOrder(
            wa_number="919800000001",
            customer_name="Analytics Buyer",
            items=[
                ParsedItem(product_retailer_id="AN-SKU-1", quantity=2,
                           unit_price=Decimal("1000"), product_name="Silk Saree"),
            ],
        )
    )
    order_service.set_payment_status(paid["id"], "paid", actor="test")
    order_service.update_order_fields(paid["id"], actor="test",
                                      city="Jaipur", state="Rajasthan")

    delivered = order_service.create_order(
        ParsedOrder(
            wa_number="919800000002",
            customer_name="Second Buyer",
            items=[
                ParsedItem(product_retailer_id="AN-SKU-2", quantity=1,
                           unit_price=Decimal("500"), product_name="Cotton Kurta"),
            ],
        )
    )
    order_service.set_status(delivered["id"], "delivered", actor="test")
    return {"paid": paid, "delivered": delivered}


# --------------------------------------------------------------------------
# commerce/analytics.py
# --------------------------------------------------------------------------

def test_order_summary_shape(seeded):
    summary = analytics.order_summary()
    expected = {
        "today_orders", "month_orders", "pending_orders", "delivered_orders",
        "cancelled_orders", "revenue", "avg_order_value", "total_orders",
    }
    assert expected <= set(summary.keys())
    assert summary["total_orders"] >= 1
    assert summary["delivered_orders"] >= 1
    # Revenue counts the paid order + the delivered order.
    assert summary["revenue"] >= 1.0
    assert isinstance(summary["avg_order_value"], float)


def test_top_products(seeded):
    products = analytics.top_products()
    assert isinstance(products, list)
    assert products, "expected at least one product row"
    row = products[0]
    assert {"name", "qty", "revenue"} <= set(row.keys())
    assert row["qty"] >= 1


def test_top_customers(seeded):
    customers = analytics.top_customers()
    assert isinstance(customers, list)
    assert customers
    assert {"wa_number", "customer_name", "orders", "spend"} <= set(customers[0].keys())


def test_sales_by_region(seeded):
    regions = analytics.sales_by_region()
    assert set(regions.keys()) == {"by_state", "by_city"}
    assert isinstance(regions["by_state"], list)
    assert isinstance(regions["by_city"], list)


def test_time_series(seeded):
    daily = analytics.daily_series(30)
    monthly = analytics.monthly_series(12)
    yearly = analytics.yearly_series()
    assert len(daily) == 30
    assert len(monthly) == 12
    assert isinstance(yearly, list) and yearly
    for row in (daily[0], monthly[0], yearly[0]):
        assert {"period", "orders", "revenue"} <= set(row.keys())


def test_conversion_rate(seeded):
    rate = analytics.conversion_rate()
    assert isinstance(rate, float)
    assert 0.0 <= rate <= 100.0


def test_analytics_bundle(seeded):
    bundle = analytics.analytics_bundle()
    assert {"summary", "top_products", "top_customers", "regions",
            "daily", "monthly", "yearly", "conversion_rate"} <= set(bundle.keys())


def test_analytics_functions_never_raise():
    # Even called bare (module import order), they must return safe defaults.
    assert isinstance(analytics.order_summary(), dict)
    assert isinstance(analytics.top_products(), list)


# --------------------------------------------------------------------------
# admin/orders_routes.py blueprint wiring
# --------------------------------------------------------------------------

def test_blueprint_registers_expected_routes():
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(admin_orders_bp)

    assert admin_orders_bp.name == "admin_orders"
    rules = {r.rule for r in app.url_map.iter_rules()}
    for expected in {
        "/admin/commerce/orders",
        "/admin/commerce/orders/<int:order_id>",
        "/admin/commerce/orders/<int:order_id>/action",
        "/admin/commerce/orders/export",
        "/admin/commerce/analytics",
        "/admin/commerce/api/analytics",
    }:
        assert expected in rules, f"missing route {expected}"

    # The action route must accept POST.
    action_methods = {
        m
        for r in app.url_map.iter_rules()
        if r.rule.endswith("/action")
        for m in r.methods
    }
    assert "POST" in action_methods
