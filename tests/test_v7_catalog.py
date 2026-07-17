"""
tests/test_v7_catalog.py
-------------------------
Tests for the v7.0 merchandising layer: wishlists (``commerce/wishlist.py``),
bundles (``commerce/bundles.py``), carts + abandoned-cart recovery
(``commerce/carts.py``) and the ``admin/catalog_routes.py`` blueprint.

No-network, no-mock tests: the commerce DB is bootstrapped through
:func:`commerce.bootstrap`, unique WhatsApp numbers keep aggregates
deterministic across repeated runs against the persistent SQLite file, and the
blueprint is verified by registering it on a throwaway Flask app.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

from datetime import datetime, timedelta, timezone

from flask import Flask

import commerce
from commerce import bundles as bundles_service
from commerce import carts as carts_service
from commerce import wishlist as wishlist_service
from admin.catalog_routes import admin_catalog_bp


def _unique_wa() -> str:
    return "9199" + f"{uuid.uuid4().int % 10**8:08d}"


def setup_module(module):
    commerce.bootstrap()


# --------------------------------------------------------------------------
# Wishlist
# --------------------------------------------------------------------------

def test_wishlist_add_idempotent_and_list():
    wa = _unique_wa()
    first = wishlist_service.add_item(wa, "WL-SKU-1", product_name="Saree", price=1200)
    assert "error" not in first
    # Adding the same product again is idempotent (unique constraint respected).
    second = wishlist_service.add_item(wa, "WL-SKU-1", product_name="Saree", price=1200)
    assert second["id"] == first["id"]

    wishlist_service.add_item(wa, "WL-SKU-2", product_name="Kurta", price=800)
    items = wishlist_service.list_items(wa)
    assert len(items) == 2
    ids = {i["product_retailer_id"] for i in items}
    assert ids == {"WL-SKU-1", "WL-SKU-2"}

    # Removal works and is reflected in the list.
    assert wishlist_service.remove_item(wa, "WL-SKU-1") is True
    assert wishlist_service.remove_item(wa, "WL-SKU-1") is False
    assert len(wishlist_service.list_items(wa)) == 1

    # Admin aggregate views.
    assert isinstance(wishlist_service.list_all(limit=10, offset=0), list)
    assert wishlist_service.count_all() >= 1


# --------------------------------------------------------------------------
# Bundles
# --------------------------------------------------------------------------

def test_create_bundle_and_list():
    sku = "BND-" + uuid.uuid4().hex[:8]
    bundle = bundles_service.create_bundle(
        "Festive Combo",
        2500,
        [{"retailer_id": "B-SKU-1", "qty": 2}, {"retailer_id": "B-SKU-2"}],
        sku=sku,
        currency="INR",
    )
    assert "error" not in bundle
    assert bundle["sku"] == sku
    assert bundle["price"] == 2500.0
    assert bundle["items"] == [
        {"retailer_id": "B-SKU-1", "qty": 2},
        {"retailer_id": "B-SKU-2", "qty": 1},
    ]

    # Fetchable by id and by sku.
    assert bundles_service.get_bundle(bundle["id"])["sku"] == sku
    assert bundles_service.get_bundle(sku)["id"] == bundle["id"]

    active = bundles_service.list_bundles(active=True)
    assert any(b["id"] == bundle["id"] for b in active)

    # Deactivation removes it from the active set.
    bundles_service.deactivate_bundle(bundle["id"])
    assert bundles_service.get_bundle(bundle["id"])["active"] is False
    active_after = bundles_service.list_bundles(active=True)
    assert all(b["id"] != bundle["id"] for b in active_after)


# --------------------------------------------------------------------------
# Carts + abandoned-cart recovery
# --------------------------------------------------------------------------

def test_upsert_cart_single_active_per_customer():
    wa = _unique_wa()
    first = carts_service.upsert_cart(wa, [{"retailer_id": "C-1", "qty": 1}], 500)
    second = carts_service.upsert_cart(
        wa, [{"retailer_id": "C-1", "qty": 3}], 1500
    )
    # Same cart row is reused; only one active cart exists.
    assert second["id"] == first["id"]
    assert second["subtotal"] == 1500.0
    active = carts_service.get_active_cart(wa)
    assert active["id"] == first["id"]
    assert active["items"] == [{"retailer_id": "C-1", "qty": 3}]


def test_find_abandoned_fresh_vs_old():
    wa = _unique_wa()
    carts_service.upsert_cart(wa, [{"retailer_id": "C-9", "qty": 1}], 999)

    # A fresh cart is NOT abandoned under the default window.
    fresh = carts_service.find_abandoned()
    assert all(c["wa_number"] != wa for c in fresh)

    # hours=0 treats every un-nudged active cart as abandoned.
    zero = carts_service.find_abandoned(hours=0)
    assert any(c["wa_number"] == wa for c in zero)

    # Ageing the cart's updated_at makes it appear under the default window too.
    _age_cart(wa, hours=48)
    aged = carts_service.find_abandoned()
    assert any(c["wa_number"] == wa for c in aged)


def test_recover_abandoned_carts(monkeypatch):
    wa = _unique_wa()
    carts_service.upsert_cart(wa, [{"retailer_id": "C-7", "qty": 2}], 1400)
    _age_cart(wa, hours=48)

    sent = []

    def _fake_send(to_number, message):
        sent.append((to_number, message))
        return True

    # send_text_message is imported lazily inside recover_abandoned_carts.
    monkeypatch.setattr("whatsapp.sender.send_text_message", _fake_send)

    count = carts_service.recover_abandoned_carts()
    assert count >= 1
    assert any(to == wa for to, _ in sent)

    # The cart is now stamped and no longer resurfaces as abandoned.
    cart = carts_service.get_active_cart(wa)
    assert cart["recovery_sent_at"] is not None
    assert all(c["wa_number"] != wa for c in carts_service.find_abandoned())


def test_mark_converted():
    wa = _unique_wa()
    carts_service.upsert_cart(wa, [{"retailer_id": "C-3", "qty": 1}], 300)
    converted = carts_service.mark_converted(wa, order_id=12345)
    assert converted["status"] == "converted"
    assert converted["order_id"] == 12345
    assert carts_service.get_active_cart(wa) is None


def _age_cart(wa_number: str, *, hours: int) -> None:
    """Backdate a customer's active cart's ``updated_at`` for abandonment tests."""
    from database.db import session_scope
    from database.models import Cart

    with session_scope() as session:
        cart = (
            session.query(Cart)
            .filter_by(wa_number=wa_number, status="active")
            .order_by(Cart.id.desc())
            .first()
        )
        cart.updated_at = datetime.now(timezone.utc) - timedelta(hours=hours)


# --------------------------------------------------------------------------
# admin/catalog_routes.py blueprint wiring
# --------------------------------------------------------------------------

def test_blueprint_registers_expected_routes():
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(admin_catalog_bp)

    assert admin_catalog_bp.name == "admin_catalog"
    rules = {r.rule for r in app.url_map.iter_rules()}
    for expected in {
        "/admin/catalog/bundles",
        "/admin/catalog/bundles/new",
        "/admin/catalog/bundles/<int:bid>/deactivate",
        "/admin/catalog/wishlist",
        "/admin/catalog/carts",
    }:
        assert expected in rules, f"missing route {expected}"

    new_methods = {
        m
        for r in app.url_map.iter_rules()
        if r.rule.endswith("/bundles/new")
        for m in r.methods
    }
    assert {"GET", "POST"} <= new_methods
