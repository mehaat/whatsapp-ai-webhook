"""
commerce/carts.py
------------------
The v7.0 working-cart store and abandoned-cart recovery.

One *active* cart is kept per customer (``wa_number``); it is upserted as items
change, marked ``converted`` when an order is placed, and — when it has sat idle
past ``config.abandoned_cart_hours`` without a recovery nudge — surfaced by
:func:`find_abandoned` and reminded by :func:`recover_abandoned_carts` (wired to
a scheduled job elsewhere).

Every public function is defensive — it returns a plain, serializable value and
never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from config import config
from database.db import session_scope
from database.models import Cart
from utils.logging import logger


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _f(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _load_items(raw: Optional[str]) -> List[Dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _to_dict(c: Cart) -> Dict[str, Any]:
    return {
        "id": c.id,
        "wa_number": c.wa_number,
        "status": c.status,
        "currency": c.currency,
        "items": _load_items(c.items),
        "subtotal": _f(c.subtotal),
        "order_id": c.order_id,
        "recovery_sent_at": _iso(c.recovery_sent_at),
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }


def upsert_cart(
    wa_number: str,
    items: List[Dict[str, Any]],
    subtotal: Any,
    currency: str = "INR",
) -> Dict[str, Any]:
    """Create or update the single active cart for a customer. Never raises."""
    try:
        with session_scope() as session:
            cart = (
                session.query(Cart)
                .filter_by(wa_number=wa_number, status="active")
                .order_by(Cart.id.desc())
                .first()
            )
            if cart is None:
                cart = Cart(
                    wa_number=wa_number,
                    status="active",
                    currency=currency or "INR",
                    items=json.dumps(items or []),
                    subtotal=_to_decimal(subtotal),
                )
                session.add(cart)
            else:
                cart.items = json.dumps(items or [])
                cart.subtotal = _to_decimal(subtotal)
                cart.currency = currency or cart.currency
                cart.updated_at = _utcnow()
                # A re-activated cart clears any prior recovery nudge.
                cart.recovery_sent_at = None
            session.flush()
            return _to_dict(cart)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | upsert_cart failed for %s: %s", wa_number, exc)
        return {"error": "upsert_failed", "detail": str(exc)}


def mark_converted(wa_number: str, order_id: int) -> Optional[Dict[str, Any]]:
    """Mark a customer's active cart as converted to an order. Never raises."""
    try:
        with session_scope() as session:
            cart = (
                session.query(Cart)
                .filter_by(wa_number=wa_number, status="active")
                .order_by(Cart.id.desc())
                .first()
            )
            if cart is None:
                return None
            cart.status = "converted"
            cart.order_id = order_id
            session.flush()
            return _to_dict(cart)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | mark_converted failed for %s: %s", wa_number, exc)
        return None


def get_active_cart(wa_number: str) -> Optional[Dict[str, Any]]:
    """Return a customer's active cart, or ``None``. Never raises."""
    try:
        with session_scope() as session:
            cart = (
                session.query(Cart)
                .filter_by(wa_number=wa_number, status="active")
                .order_by(Cart.id.desc())
                .first()
            )
            return _to_dict(cart) if cart is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | get_active_cart failed for %s: %s", wa_number, exc)
        return None


def list_carts(status: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Return carts newest-first, optionally filtered by status. Never raises."""
    try:
        with session_scope() as session:
            q = session.query(Cart)
            if status:
                q = q.filter(Cart.status == status)
            q = q.order_by(Cart.updated_at.desc(), Cart.id.desc()).limit(limit).offset(offset)
            return [_to_dict(c) for c in q.all()]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | list_carts failed: %s", exc)
        return []


def find_abandoned(hours: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return active carts idle past ``hours`` with no recovery sent yet.

    ``hours`` defaults to ``config.abandoned_cart_hours``. Passing ``hours=0``
    treats every un-nudged active cart as abandoned (cutoff = now). Never raises.
    """
    try:
        window = config.abandoned_cart_hours if hours is None else hours
        cutoff = _utcnow() - timedelta(hours=window)
        with session_scope() as session:
            rows = (
                session.query(Cart)
                .filter(
                    Cart.status == "active",
                    Cart.recovery_sent_at.is_(None),
                    Cart.updated_at <= cutoff,
                )
                .order_by(Cart.updated_at.asc(), Cart.id.asc())
                .all()
            )
            return [_to_dict(c) for c in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | find_abandoned failed: %s", exc)
        return []


def mark_recovery_sent(cart_id: int) -> Optional[Dict[str, Any]]:
    """Stamp a cart's ``recovery_sent_at`` so it is not nudged twice. Never raises."""
    try:
        with session_scope() as session:
            cart = session.get(Cart, cart_id)
            if cart is None:
                return None
            cart.recovery_sent_at = _utcnow()
            session.flush()
            return _to_dict(cart)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | mark_recovery_sent failed for #%s: %s", cart_id, exc)
        return None


def recover_abandoned_carts() -> int:
    """Send a friendly WhatsApp reminder for each abandoned cart. Never raises.

    Best-effort: a send failure for one cart never aborts the batch. Returns the
    number of carts for which a reminder was dispatched (and stamped).
    """
    sent = 0
    try:
        carts = find_abandoned()
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | recover_abandoned_carts: find failed: %s", exc)
        return 0

    for cart in carts:
        wa_number = cart.get("wa_number")
        if not wa_number:
            continue
        try:
            from whatsapp.sender import send_text_message

            message = _recovery_message(cart)
            ok = send_text_message(wa_number, message)
        except Exception as exc:  # noqa: BLE001 - one bad send must not stop the batch
            logger.debug("COMMERCE | recovery send failed for %s: %s", wa_number, exc)
            ok = False

        # Stamp regardless so we never spam a customer on repeated runs.
        mark_recovery_sent(cart["id"])
        if ok:
            sent += 1
        try:
            from commerce.service import order_service

            order_service.log_notification(
                kind="cart_recovery", wa_number=wa_number, audience="customer",
                body=_recovery_message(cart), status="sent" if ok else "failed",
            )
        except Exception:  # noqa: BLE001
            pass

    if sent:
        logger.info("COMMERCE | Abandoned-cart recovery reminded %d customer(s)", sent)
    return sent


def _recovery_message(cart: Dict[str, Any]) -> str:
    """A short, friendly cart-recovery nudge."""
    currency = cart.get("currency") or "INR"
    subtotal = cart.get("subtotal") or 0
    return (
        "Hi! 👋 You left some lovely items in your ME-HAAT cart. "
        f"They're still waiting for you (subtotal {currency} {subtotal:.2f}). "
        "Reply here to complete your order — we'd love to get them to you! 🛍️"
    )


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
