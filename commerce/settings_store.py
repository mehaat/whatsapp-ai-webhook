"""
commerce/settings_store.py
---------------------------
A DB-backed key/value settings store layered over the shared
:class:`~database.models.Settings` table (using the shop-agnostic
``shop_domain IS NULL`` rows).

These are *runtime overrides* surfaced in the admin Settings UI. Environment
variables (via :mod:`config`) remain the boot-time default for every setting;
a value written here simply overrides that default at read time for callers
that opt into :func:`get_setting`. This module never mutates ``config`` and
never raises — a storage failure degrades to the provided ``default``.

The ``settings`` unique constraint is ``(shop_domain, key)``; because SQLite
treats NULLs as distinct, upserts here always resolve the existing global row
explicitly before inserting.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from database.db import session_scope
from database.models import Settings
from utils.logging import logger


# --------------------------------------------------------------------------
# Curated, UI-editable settings.
# --------------------------------------------------------------------------
# Each entry: {key, label, type, help}. ``type`` drives the form widget
# ("text" | "number" | "bool" | "select") in the Settings template. These are
# DB overrides surfaced in the UI; the matching env vars remain the boot default.
EDITABLE_SETTINGS: List[Dict[str, str]] = [
    {
        "key": "business_name",
        "label": "Business name",
        "type": "text",
        "help": "Display name used on invoices and customer messages.",
    },
    {
        "key": "business_gstin",
        "label": "Business GSTIN",
        "type": "text",
        "help": "GST identification number printed on tax invoices.",
    },
    {
        "key": "delivery_estimate",
        "label": "Delivery estimate",
        "type": "text",
        "help": "Human text shown to customers, e.g. '3–5 business days'.",
    },
    {
        "key": "low_stock_threshold",
        "label": "Low-stock threshold",
        "type": "number",
        "help": "Units at or below which a product is flagged low-stock.",
    },
    {
        "key": "coupons_enabled",
        "label": "Coupons enabled",
        "type": "bool",
        "help": "Allow customers to apply discount coupons at checkout.",
    },
    {
        "key": "auto_draft_order",
        "label": "Auto-create Shopify draft order",
        "type": "bool",
        "help": "Automatically create a Shopify draft order for new orders.",
    },
    {
        "key": "payment_provider",
        "label": "Payment provider",
        "type": "text",
        "help": "Active payment provider key, e.g. 'razorpay' or 'stripe'.",
    },
    {
        "key": "shipping_provider",
        "label": "Shipping provider",
        "type": "text",
        "help": "Active shipping/courier provider key, e.g. 'shiprocket'.",
    },
    {
        "key": "abandoned_cart_hours",
        "label": "Abandoned-cart delay (hours)",
        "type": "number",
        "help": "Hours of inactivity before a cart is treated as abandoned.",
    },
]

# Fast membership set of editable keys.
EDITABLE_KEYS = tuple(item["key"] for item in EDITABLE_SETTINGS)


# --------------------------------------------------------------------------
# Read / write
# --------------------------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return the stored override for ``key``, or ``default`` if unset.

    Never raises; a storage error degrades to ``default``.
    """
    key = (key or "").strip()
    if not key:
        return default
    try:
        with session_scope() as session:
            row = (
                session.query(Settings)
                .filter(Settings.shop_domain.is_(None), Settings.key == key)
                .first()
            )
            if row is None or row.value is None:
                return default
            return row.value
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | get_setting failed for %r: %s", key, exc)
        return default


def set_setting(key: str, value: Any, actor: str = "admin") -> None:
    """Upsert an override for ``key`` (stored as text). Never raises."""
    key = (key or "").strip()
    if not key:
        return
    text = "" if value is None else str(value)
    try:
        with session_scope() as session:
            row = (
                session.query(Settings)
                .filter(Settings.shop_domain.is_(None), Settings.key == key)
                .first()
            )
            if row is None:
                row = Settings(shop_domain=None, key=key, value=text)
                session.add(row)
            else:
                row.value = text
        logger.info("COMMERCE | setting %s updated by %s", key, actor)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | set_setting failed for %r: %s", key, exc)


def all_settings() -> Dict[str, Optional[str]]:
    """Return every stored global override as a ``{key: value}`` dict.

    Never raises; returns ``{}`` on error.
    """
    try:
        with session_scope() as session:
            rows = (
                session.query(Settings)
                .filter(Settings.shop_domain.is_(None))
                .all()
            )
            return {row.key: row.value for row in rows}
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | all_settings failed: %s", exc)
        return {}
