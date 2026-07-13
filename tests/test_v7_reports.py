"""
tests/test_v7_reports.py
-------------------------
Tests for the v7.0 business reports in :mod:`commerce.reports`.

No-network, no-mock: the commerce DB is bootstrapped via
:func:`commerce.bootstrap` and real orders are created through
``order_service``. Because the default SQLite DB is shared across the suite, the
assertions check (a) that this test's own orders appear with correct per-row
values and (b) that each report's summary is internally consistent with its
rows — rather than relying on absolute, isolated totals.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

from datetime import datetime, timezone
from decimal import Decimal

import pytest

import commerce
from commerce import reports
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service


def _unique_wa() -> str:
    return "9197" + f"{uuid.uuid4().int % 10**8:08d}"


@pytest.fixture(scope="module")
def orders() -> list[dict]:
    """Bootstrap the DB and create two orders with explicit discount + tax."""
    commerce.bootstrap()
    created = []
    for i in range(2):
        o = order_service.create_order(
            ParsedOrder(
                wa_number=_unique_wa(),
                customer_name=f"Report Buyer {i}",
                items=[
                    ParsedItem(
                        product_retailer_id=f"RPT-SKU-{uuid.uuid4().hex[:6]}",
                        quantity=2,
                        unit_price=Decimal("500"),
                        product_name="Report Kurti",
                    ),
                ],
            ),
            discount=Decimal("100"),
            tax=Decimal("90"),
        )
        # Mark one paid so revenue-recognition is exercised.
        if i == 0:
            order_service.set_payment_status(o["id"], "paid", actor="test")
        created.append(o)
    return created


# --------------------------------------------------------------------------
# GST report
# --------------------------------------------------------------------------

def test_gst_report_shape_and_values(orders):
    report = reports.gst_report()
    assert report["columns"] == ["order_number", "date", "customer", "taxable_value", "tax", "total"]
    assert isinstance(report["rows"], list) and report["rows"]
    assert isinstance(report["summary"], dict)

    by_number = {row[0]: row for row in report["rows"]}
    for o in orders:
        assert o["order_number"] in by_number, "created order missing from GST report"
        row = by_number[o["order_number"]]
        expected_taxable = round(o["subtotal"] - o["discount"], 2)
        assert row[3] == expected_taxable
        assert row[4] == round(o["tax"], 2)
        assert row[5] == round(o["total_amount"], 2)

    # Summary is internally consistent with the rows.
    s = report["summary"]
    assert s["orders"] == len(report["rows"])
    assert abs(s["taxable_value"] - sum(r[3] for r in report["rows"])) < 0.01
    assert abs(s["tax"] - sum(r[4] for r in report["rows"])) < 0.01
    assert abs(s["total"] - sum(r[5] for r in report["rows"])) < 0.01


def test_gst_report_future_range_is_empty(orders):
    report = reports.gst_report(date_from="2999-01-01", date_to="2999-12-31")
    assert report["rows"] == []
    assert report["summary"]["orders"] == 0


# --------------------------------------------------------------------------
# Sales report
# --------------------------------------------------------------------------

def test_sales_report_groups_by_day(orders):
    report = reports.sales_report(group="day")
    assert report["columns"] == ["period", "orders", "revenue", "avg_order_value"]
    assert report["summary"]["group"] == "day"

    today = datetime.now(timezone.utc).date().isoformat()
    periods = {row[0] for row in report["rows"]}
    assert today in periods, "today's orders should form a daily bucket"

    today_row = next(row for row in report["rows"] if row[0] == today)
    assert today_row[1] >= 2  # at least our two orders counted
    assert today_row[2] >= 0  # revenue is non-negative


def test_sales_report_groups_by_month(orders):
    report = reports.sales_report(group="month")
    assert report["summary"]["group"] == "month"
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert any(row[0] == this_month for row in report["rows"])


# --------------------------------------------------------------------------
# Inventory / customer / product reports
# --------------------------------------------------------------------------

def test_inventory_report_shape(orders):
    report = reports.inventory_report()
    assert set(report["columns"]) == {"product", "variant", "reserved", "committed", "ordered_qty"}
    assert isinstance(report["rows"], list)
    assert isinstance(report["summary"], dict)
    assert "ordered_qty" in report["summary"]
    assert "top_ordered" in report["summary"]
    # Our order items pushed total ordered quantity above zero.
    assert report["summary"]["ordered_qty"] >= 4


def test_customer_report_shape(orders):
    report = reports.customer_report()
    assert report["columns"] == ["customer", "wa_number", "orders", "revenue", "last_order"]
    assert report["rows"]
    wa_numbers = {row[1] for row in report["rows"]}
    for o in orders:
        assert o["wa_number"] in wa_numbers


def test_product_report_shape(orders):
    report = reports.product_report()
    assert report["columns"] == ["product", "retailer_id", "qty", "revenue", "orders"]
    assert isinstance(report["rows"], list) and report["rows"]
    assert report["summary"]["qty"] >= 4


def test_run_report_dispatch(orders):
    assert reports.run_report("gst")["columns"][0] == "order_number"
    assert reports.run_report("inventory")["columns"][0] == "product"
    assert reports.run_report("sales", group="month")["summary"]["group"] == "month"
    # Unknown report degrades gracefully.
    unknown = reports.run_report("nope")
    assert unknown["rows"] == []
