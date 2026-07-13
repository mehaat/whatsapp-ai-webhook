"""
tests/test_v7_shipping.py
--------------------------
Unit tests for the v7.0 fulfilment & shipping system.

Every test runs fully offline: only the always-available :class:`ManualProvider`
is exercised, so no HTTP request is ever made. Persistence uses the default
SQLite ``DATABASE_URL`` via ``commerce.bootstrap()`` (which creates the
``shipments`` table alongside the rest of the commerce schema).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import commerce
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_commerce():
    """Ensure the commerce + shipping schema exists before any test runs."""
    commerce.bootstrap()
    yield


def _make_order() -> dict:
    """Persist a representative order and return its serialized dict."""
    parsed = ParsedOrder(
        wa_number="919812345670",
        wa_order_id="wamid.SHIPTEST1",
        catalog_id="CATALOG1",
        customer_name="Test Buyer",
        currency="INR",
        items=[
            ParsedItem(
                product_retailer_id="SAREE-RED-1",
                quantity=2,
                unit_price=Decimal("1500.00"),
                currency="INR",
                product_name="Bandhani Silk Saree",
                variant="Maroon",
            ),
            ParsedItem(
                product_retailer_id="JHUMKA-1",
                quantity=1,
                unit_price=Decimal("650.00"),
                currency="INR",
                product_name="Kundan Jhumka",
                variant="Gold",
            ),
        ],
        note="Deliver fast",
    )
    return order_service.create_order(parsed, status="confirmed", actor="test")


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

def test_factory_manual_provider_type_and_registry():
    from shipping.factory import available_providers, get_provider
    from shipping.manual import ManualProvider

    provider = get_provider("manual")
    assert isinstance(provider, ManualProvider)
    assert provider.name == "manual"

    names = available_providers()
    assert "manual" in names
    assert "shiprocket" in names
    assert "delhivery" in names


def test_factory_unknown_falls_back_to_manual():
    from shipping.factory import get_provider
    from shipping.manual import ManualProvider

    provider = get_provider("no-such-courier")
    assert isinstance(provider, ManualProvider)


def test_manual_provider_generates_awb_and_tracks():
    from shipping.factory import get_provider

    provider = get_provider("manual")
    result = provider.create_shipment({"id": 42, "order_number": "MH-1"})
    assert result.ok is True
    assert result.provider == "manual"
    assert result.awb and result.awb.startswith("MH")
    assert result.tracking_url

    tracking = provider.track(result.awb)
    assert tracking.ok is True
    assert tracking.status


# --------------------------------------------------------------------------
# Orchestration service
# --------------------------------------------------------------------------

def test_create_shipment_for_order_persists_and_ships(monkeypatch):
    order = _make_order()
    order_id = order["id"]

    from shipping.service import create_shipment_for_order, get_shipment

    result = create_shipment_for_order(order_id)
    assert result["ok"] is True, result
    shipment = result["shipment"]
    assert shipment is not None
    assert shipment["order_id"] == order_id
    assert shipment["provider"] == "manual"
    assert shipment["awb"] and shipment["awb"].startswith("MH")

    # The Shipment row is readable back out.
    fetched = get_shipment(shipment["id"])
    assert fetched is not None
    assert fetched["awb"] == shipment["awb"]

    # The order moved to 'shipped' and recorded the courier + AWB.
    updated = order_service.get_order(order_id=order_id)
    assert updated["status"] == "shipped"
    assert updated["tracking_number"] == shipment["awb"]
    assert updated["courier"]


def test_list_shipments_returns_created_row(monkeypatch):
    order = _make_order()
    from shipping.service import create_shipment_for_order, list_shipments

    created = create_shipment_for_order(order["id"], provider_name="manual")
    assert created["ok"] is True

    rows = list_shipments(limit=100)
    assert any(r["id"] == created["shipment"]["id"] for r in rows)


def test_track_shipment_by_awb_offline(monkeypatch):
    order = _make_order()
    from shipping.service import create_shipment_for_order, track_shipment

    created = create_shipment_for_order(order["id"], provider_name="manual")
    awb = created["shipment"]["awb"]

    tracking = track_shipment(awb=awb)
    assert tracking["ok"] is True
    assert tracking["status"]


def test_schedule_pickup_manual(monkeypatch):
    order = _make_order()
    from shipping.service import create_shipment_for_order, schedule_pickup

    created = create_shipment_for_order(order["id"], provider_name="manual")
    result = schedule_pickup(created["shipment"]["id"])
    assert result["ok"] is True
    assert result["shipment"]["pickup_scheduled_at"]


# --------------------------------------------------------------------------
# Packing / label PDFs
# --------------------------------------------------------------------------

def test_generate_packing_slip_writes_real_pdf():
    from commerce.packing import generate_packing_slip

    order = _make_order()
    result = generate_packing_slip(order)
    path = result["path"]
    with open(path, "rb") as fh:
        header = fh.read(5)
    assert header == b"%PDF-"


def test_generate_shipping_label_writes_real_pdf(monkeypatch):
    order = _make_order()
    from shipping.service import create_shipment_for_order
    from commerce.packing import generate_shipping_label

    created = create_shipment_for_order(order["id"], provider_name="manual")
    result = generate_shipping_label(order, created["shipment"])
    path = result["path"]
    with open(path, "rb") as fh:
        header = fh.read(5)
    assert header == b"%PDF-"
