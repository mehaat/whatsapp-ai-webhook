"""
tests/test_v61_reservations.py
-------------------------------
Tests for the v6.1 inventory reservation ledger (``commerce/reservations.py``).

Persistence uses the default SQLite database bootstrapped through
:func:`commerce.bootstrap`. Inventory sync stays disabled so no network is
touched: reservations are exercised purely against the local ledger.

Retailer id ``"123"`` is fully numeric, so ``resolve_variant_id`` maps it to the
int variant id ``123`` — asserted below.
"""

from __future__ import annotations

import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

import commerce
from commerce.service import order_service
from commerce.schema import ParsedOrder, ParsedItem
from commerce import reservations


commerce.bootstrap()


def _make_order(wa_number: str) -> dict:
    """Create a persisted order with one line for retailer id '123' (variant 123)."""
    parsed = ParsedOrder(
        wa_number=wa_number,
        wa_order_id=f"wamid.{wa_number}",
        customer_name="Reservation Buyer",
        items=[ParsedItem("123", 2, Decimal("100"))],
    )
    order = order_service.create_order(parsed)
    # create_order returns the order with its items serialized.
    return order_service.get_order(order_id=order["id"], include_items=True)


def test_reserve_for_order_creates_reserved_rows():
    order = _make_order("919800000001")

    created = reservations.reserve_for_order(order)

    assert len(created) == 1
    row = created[0]
    assert row["status"] == "reserved"
    assert row["variant_id"] == "123"  # "123" resolves to variant 123
    assert row["quantity"] == 2
    assert row["synced_to_shopify"] is False

    stored = reservations.get_reservations(order["id"])
    assert len(stored) == 1
    assert stored[0]["status"] == "reserved"


def test_reserve_is_idempotent():
    order = _make_order("919800000002")

    first = reservations.reserve_for_order(order)
    assert len(first) == 1

    # A retry must not duplicate the ledger rows.
    second = reservations.reserve_for_order(order)
    assert second == []

    stored = reservations.get_reservations(order["id"])
    assert len(stored) == 1


def test_release_for_order_marks_released_and_counts():
    order = _make_order("919800000003")
    reservations.reserve_for_order(order)

    released = reservations.release_for_order(order["id"])
    assert released == 1

    stored = reservations.get_reservations(order["id"])
    assert all(r["status"] == "released" for r in stored)

    # Nothing left in "reserved" to release a second time.
    assert reservations.release_for_order(order["id"]) == 0


def test_commit_for_order_marks_committed():
    order = _make_order("919800000004")
    reservations.reserve_for_order(order)

    committed = reservations.commit_for_order(order["id"])
    assert committed == 1

    stored = reservations.get_reservations(order["id"])
    assert all(r["status"] == "committed" for r in stored)


def test_reserved_quantity_sums_reserved_rows():
    order = _make_order("919800000005")
    reservations.reserve_for_order(order)

    # At least the 2 units from this order are reserved against variant 123.
    assert reservations.reserved_quantity(123) >= 2
