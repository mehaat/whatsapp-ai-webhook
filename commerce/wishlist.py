"""
commerce/wishlist.py
---------------------
The v7.0 customer wishlist: products a customer saved for later, keyed by
``(wa_number, product_retailer_id)`` with a unique constraint so saving the
same item twice is idempotent.

Every public function is defensive — it returns a plain, serializable value and
never raises — so the WhatsApp webhook and the admin dashboard can call it
unconditionally.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from database.db import session_scope
from database.models import WishlistItem
from utils.logging import logger


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _to_dict(w: WishlistItem) -> Dict[str, Any]:
    return {
        "id": w.id,
        "wa_number": w.wa_number,
        "product_retailer_id": w.product_retailer_id,
        "product_name": w.product_name,
        "price": _f(w.price),
        "created_at": _iso(w.created_at),
    }


def add_item(
    wa_number: str,
    product_retailer_id: str,
    *,
    product_name: Optional[str] = None,
    price: Optional[Any] = None,
) -> Dict[str, Any]:
    """Save a product to a customer's wishlist (idempotent). Never raises.

    If the item already exists for this customer it is returned unchanged
    (optionally refreshing name/price), honouring the unique constraint.
    """
    try:
        with session_scope() as session:
            existing = (
                session.query(WishlistItem)
                .filter_by(wa_number=wa_number, product_retailer_id=product_retailer_id)
                .first()
            )
            if existing is not None:
                if product_name is not None:
                    existing.product_name = product_name
                if price is not None:
                    existing.price = _to_decimal(price)
                session.flush()
                return _to_dict(existing)
            item = WishlistItem(
                wa_number=wa_number,
                product_retailer_id=product_retailer_id,
                product_name=product_name,
                price=_to_decimal(price) if price is not None else None,
            )
            session.add(item)
            session.flush()
            result = _to_dict(item)
        logger.info("COMMERCE | Wishlist add %s for %s", product_retailer_id, wa_number)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | wishlist add_item failed: %s", exc)
        return {"error": "add_failed", "detail": str(exc)}


def remove_item(wa_number: str, product_retailer_id: str) -> bool:
    """Remove a saved item. Returns ``True`` if one was deleted. Never raises."""
    try:
        with session_scope() as session:
            item = (
                session.query(WishlistItem)
                .filter_by(wa_number=wa_number, product_retailer_id=product_retailer_id)
                .first()
            )
            if item is None:
                return False
            session.delete(item)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | wishlist remove_item failed: %s", exc)
        return False


def list_items(wa_number: str) -> List[Dict[str, Any]]:
    """Return a customer's saved items, newest-first. Never raises."""
    try:
        with session_scope() as session:
            rows = (
                session.query(WishlistItem)
                .filter_by(wa_number=wa_number)
                .order_by(WishlistItem.created_at.desc(), WishlistItem.id.desc())
                .all()
            )
            return [_to_dict(w) for w in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | wishlist list_items failed: %s", exc)
        return []


def list_all(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Admin view: every customer's saved items, newest-first. Never raises."""
    try:
        with session_scope() as session:
            rows = (
                session.query(WishlistItem)
                .order_by(WishlistItem.created_at.desc(), WishlistItem.id.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [_to_dict(w) for w in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | wishlist list_all failed: %s", exc)
        return []


def count_all() -> int:
    """Total wishlist item count across all customers. Never raises."""
    try:
        with session_scope() as session:
            return session.query(WishlistItem).count()
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | wishlist count_all failed: %s", exc)
        return 0


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
