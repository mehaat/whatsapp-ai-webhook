"""
commerce/bundles.py
--------------------
The v7.0 product bundles / combos: several catalog items sold together at a
single bundle price. The line-up is stored as a JSON array of
``{"retailer_id": ..., "qty": ...}`` on the :class:`~database.models.Bundle`
row so the schema stays simple and portable across SQLite/PostgreSQL.

Every public function is defensive — it returns a plain, serializable value and
never raises.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from database.db import session_scope
from database.models import Bundle
from utils.logging import logger


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


def _normalise_items(items: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Coerce input items to ``[{"retailer_id": str, "qty": int}]``."""
    out: List[Dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        rid = it.get("retailer_id") or it.get("product_retailer_id")
        if not rid:
            continue
        try:
            qty = int(it.get("qty") or it.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        out.append({"retailer_id": str(rid), "qty": max(1, qty)})
    return out


def _to_dict(b: Bundle) -> Dict[str, Any]:
    return {
        "id": b.id,
        "name": b.name,
        "sku": b.sku,
        "price": _f(b.price),
        "currency": b.currency,
        "items": _load_items(b.items),
        "active": bool(b.active),
        "created_at": _iso(b.created_at),
    }


def create_bundle(
    name: str,
    price: Any,
    items: List[Dict[str, Any]],
    *,
    sku: Optional[str] = None,
    currency: str = "INR",
) -> Dict[str, Any]:
    """Create a bundle. ``items`` is stored as JSON. Never raises."""
    try:
        with session_scope() as session:
            bundle = Bundle(
                name=name,
                sku=(sku or None),
                price=_to_decimal(price),
                currency=currency or "INR",
                items=json.dumps(_normalise_items(items)),
                active=True,
            )
            session.add(bundle)
            session.flush()
            result = _to_dict(bundle)
        logger.info("COMMERCE | Bundle created %r (#%s)", name, result["id"])
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | create_bundle failed: %s", exc)
        return {"error": "create_failed", "detail": str(exc)}


def list_bundles(active: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Return bundles newest-first, optionally filtered by active flag."""
    try:
        with session_scope() as session:
            q = session.query(Bundle)
            if active is not None:
                q = q.filter(Bundle.active == bool(active))
            q = q.order_by(Bundle.created_at.desc(), Bundle.id.desc())
            return [_to_dict(b) for b in q.all()]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | list_bundles failed: %s", exc)
        return []


def get_bundle(id_or_sku: Any) -> Optional[Dict[str, Any]]:
    """Fetch one bundle by numeric id or SKU. Never raises."""
    try:
        with session_scope() as session:
            bundle = _resolve(session, id_or_sku)
            return _to_dict(bundle) if bundle is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | get_bundle failed for %r: %s", id_or_sku, exc)
        return None


def update_bundle(bundle_id: int, **fields: Any) -> Dict[str, Any]:
    """Guarded field update for a bundle. Never raises.

    Accepts ``name``, ``price``, ``currency``, ``sku``, ``active`` and
    ``items`` (a list, re-serialized to JSON).
    """
    try:
        with session_scope() as session:
            bundle = session.get(Bundle, bundle_id)
            if bundle is None:
                return {"error": "bundle_not_found", "id": bundle_id}
            if "name" in fields and fields["name"] is not None:
                bundle.name = fields["name"]
            if "price" in fields and fields["price"] is not None:
                bundle.price = _to_decimal(fields["price"])
            if "currency" in fields and fields["currency"] is not None:
                bundle.currency = fields["currency"]
            if "sku" in fields:
                bundle.sku = fields["sku"] or None
            if "active" in fields and fields["active"] is not None:
                bundle.active = bool(fields["active"])
            if "items" in fields and fields["items"] is not None:
                bundle.items = json.dumps(_normalise_items(fields["items"]))
            session.flush()
            return _to_dict(bundle)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | update_bundle failed for #%s: %s", bundle_id, exc)
        return {"error": "update_failed", "detail": str(exc)}


def deactivate_bundle(bundle_id: int) -> Dict[str, Any]:
    """Soft-disable a bundle (``active=False``). Never raises."""
    return update_bundle(bundle_id, active=False)


def _resolve(session, id_or_sku: Any) -> Optional[Bundle]:
    if isinstance(id_or_sku, int) or (isinstance(id_or_sku, str) and id_or_sku.isdigit()):
        bundle = session.get(Bundle, int(id_or_sku))
        if bundle is not None:
            return bundle
    if isinstance(id_or_sku, str):
        return session.query(Bundle).filter_by(sku=id_or_sku).first()
    return None


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
