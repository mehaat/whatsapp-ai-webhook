"""
commerce/reservations.py
-------------------------
Inventory reservation ledger for ME-HAAT Fashion AI Bot v6.1.

A *reservation* records that an order line has laid claim to some quantity of a
Shopify variant. The ledger is a local, durable source of truth kept in the
``inventory_reservations`` table; it is decoupled from Shopify so it works even
when the store is offline or has not granted ``write_inventory``.

Lifecycle of a reservation row (``status`` column):

* ``reserved``  — created when an order is placed (:func:`reserve_for_order`).
* ``released``  — the hold is returned to stock on cancel/refund
  (:func:`release_for_order`).
* ``committed`` — the stock is actually consumed on fulfillment
  (:func:`commit_for_order`).

When ``config.inventory_sync_enabled`` is true (and only then), reservations are
*best-effort* mirrored to live Shopify inventory levels via
:func:`_adjust_shopify`. Sync is never allowed to block or fail the local
ledger: any Shopify error is swallowed and logged, and ``synced_to_shopify``
simply stays ``False`` for that row.

Every public function is defensive: it returns detached plain dicts / ints and
never raises to its caller.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import config
from database.db import session_scope
from database.models import InventoryReservation
from commerce.stock import resolve_variant_id
from utils.logging import logger


def _reservation_to_dict(row: InventoryReservation) -> Dict[str, Any]:
    """Serialize an :class:`InventoryReservation` ORM row to a detached dict."""
    return {
        "id": row.id,
        "order_id": row.order_id,
        "product_retailer_id": row.product_retailer_id,
        "variant_id": row.variant_id,
        "quantity": row.quantity,
        "status": row.status,
        "synced_to_shopify": bool(row.synced_to_shopify),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def reserve_for_order(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Create ``reserved`` ledger rows for every line item on ``order``.

    Idempotent: if any reservation already exists for this ``order_id`` the call
    is a no-op and returns an empty list, so a webhook retry cannot double-book
    stock. When ``config.inventory_sync_enabled`` is true each created row is
    best-effort mirrored to Shopify (inventory decremented) and flagged
    ``synced_to_shopify``.

    Args:
        order: An order dict with ``id`` (int) and ``items`` (list of dicts,
            each carrying ``product_retailer_id``, ``variant_id``, ``quantity``
            and ``product_name``).

    Returns:
        A list of the created reservation dicts (empty when disabled, when the
        order has no items, when reservations already exist, or on error).
        Never raises.
    """
    if not config.inventory_reservation_enabled:
        return []

    try:
        order_id = int(order.get("id"))
    except (TypeError, ValueError):
        logger.warning("RESERVATIONS | reserve skipped: order missing a valid id")
        return []

    items = order.get("items") or []
    if not items:
        return []

    created: List[Dict[str, Any]] = []
    try:
        with session_scope() as session:
            existing = (
                session.query(InventoryReservation)
                .filter(InventoryReservation.order_id == order_id)
                .count()
            )
            if existing:
                logger.info(
                    "RESERVATIONS | Order %s already has %d reservation(s); skipping",
                    order_id, existing,
                )
                return []

            rows: List[InventoryReservation] = []
            for item in items:
                variant_id = resolve_variant_id(item)
                quantity = int(item.get("quantity") or 0)
                row = InventoryReservation(
                    order_id=order_id,
                    product_retailer_id=item.get("product_retailer_id"),
                    variant_id=(str(variant_id) if variant_id is not None else None),
                    quantity=quantity,
                    status="reserved",
                    synced_to_shopify=False,
                )
                session.add(row)
                rows.append(row)

            session.flush()  # assign ids / timestamps

            # Best-effort Shopify decrement (reservation reduces available stock).
            if config.inventory_sync_enabled:
                for row in rows:
                    if row.variant_id and row.quantity:
                        if _adjust_shopify(int(row.variant_id), -row.quantity):
                            row.synced_to_shopify = True
                session.flush()

            created = [_reservation_to_dict(r) for r in rows]
        logger.info("RESERVATIONS | Reserved %d line(s) for order %s", len(created), order_id)
    except Exception as exc:  # noqa: BLE001 - never raise to callers
        logger.error("RESERVATIONS | reserve_for_order failed for %s: %s", order.get("id"), exc)
        return []

    return created


def release_for_order(order_id: int) -> int:
    """Release every ``reserved`` row for ``order_id`` back to stock.

    Marks matching rows ``released`` (used on cancel/refund). When a row was
    synced to Shopify, the reserved quantity is best-effort incremented back.

    Args:
        order_id: The internal order id whose holds should be released.

    Returns:
        The number of reservations transitioned to ``released`` (0 on error or
        when there is nothing to release). Never raises.
    """
    return _transition(order_id, new_status="released", restock=True)


def commit_for_order(order_id: int) -> int:
    """Commit every ``reserved`` row for ``order_id`` (stock consumed).

    Marks matching rows ``committed`` on fulfillment. No Shopify adjustment is
    made: a committed reservation simply finalizes a decrement that already
    happened when the hold was placed (if syncing was on).

    Args:
        order_id: The internal order id whose holds should be committed.

    Returns:
        The number of reservations transitioned to ``committed`` (0 on error or
        when there is nothing to commit). Never raises.
    """
    return _transition(order_id, new_status="committed", restock=False)


def _transition(order_id: int, *, new_status: str, restock: bool) -> int:
    """Move all ``reserved`` rows for ``order_id`` to ``new_status``.

    Args:
        order_id: The order whose reserved rows are transitioned.
        new_status: Target status (``released`` or ``committed``).
        restock: When True, best-effort re-increment Shopify for synced rows.

    Returns:
        Count of rows updated. Never raises.
    """
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        logger.warning("RESERVATIONS | %s skipped: invalid order_id %r", new_status, order_id)
        return 0

    count = 0
    try:
        with session_scope() as session:
            rows = (
                session.query(InventoryReservation)
                .filter(
                    InventoryReservation.order_id == oid,
                    InventoryReservation.status == "reserved",
                )
                .all()
            )
            for row in rows:
                if restock and config.inventory_sync_enabled and row.synced_to_shopify:
                    if row.variant_id and row.quantity:
                        # Best-effort; ledger transition proceeds regardless.
                        _adjust_shopify(int(row.variant_id), row.quantity)
                row.status = new_status
                count += 1
            session.flush()
        if count:
            logger.info("RESERVATIONS | %s %d reservation(s) for order %s",
                        new_status, count, oid)
    except Exception as exc:  # noqa: BLE001 - never raise to callers
        logger.error("RESERVATIONS | %s_for_order failed for %s: %s", new_status, order_id, exc)
        return 0

    return count


def get_reservations(order_id: int) -> List[Dict[str, Any]]:
    """Return all reservation rows for ``order_id`` as detached dicts.

    Args:
        order_id: The internal order id to look up.

    Returns:
        A list of reservation dicts (any status), oldest first. Empty on error
        or when the order has no reservations. Never raises.
    """
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return []

    try:
        with session_scope() as session:
            rows = (
                session.query(InventoryReservation)
                .filter(InventoryReservation.order_id == oid)
                .order_by(InventoryReservation.id.asc())
                .all()
            )
            return [_reservation_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001 - never raise to callers
        logger.error("RESERVATIONS | get_reservations failed for %s: %s", order_id, exc)
        return []


def reserved_quantity(variant_id: Any) -> int:
    """Sum the held quantity across all ``reserved`` rows for a variant.

    Handy for reporting how much of a variant is currently spoken for but not
    yet committed or released.

    Args:
        variant_id: The Shopify variant id (int or str).

    Returns:
        The total reserved quantity (0 when none or on error). Never raises.
    """
    if variant_id is None:
        return 0
    try:
        with session_scope() as session:
            rows = (
                session.query(InventoryReservation.quantity)
                .filter(
                    InventoryReservation.variant_id == str(variant_id),
                    InventoryReservation.status == "reserved",
                )
                .all()
            )
            return sum(int(q or 0) for (q,) in rows)
    except Exception as exc:  # noqa: BLE001 - never raise to callers
        logger.error("RESERVATIONS | reserved_quantity failed for %s: %s", variant_id, exc)
        return 0


def _adjust_shopify(variant_id: int, delta: int) -> bool:
    """Best-effort adjustment of a Shopify variant's available inventory.

    Resolves the variant's ``inventory_item_id`` and the shop's first location,
    then posts ``available_adjustment=delta`` to ``inventory_levels/adjust.json``.
    A positive ``delta`` restocks; a negative ``delta`` reserves.

    Fully guarded: any missing data, unreachable shop, or API error results in a
    ``False`` return and a log line. This function is only invoked when
    ``config.inventory_sync_enabled`` is true and must never block or fail the
    local ledger.

    Args:
        variant_id: The Shopify variant id to adjust.
        delta: The signed change to apply to available inventory.

    Returns:
        True only when Shopify confirmed the adjustment; False otherwise.
    """
    try:
        from shopify.client import get_client_for_shop

        client = get_client_for_shop()
        if client is None:
            logger.info("RESERVATIONS | Shopify sync skipped: no client available")
            return False

        variant = (client.get(f"variants/{variant_id}.json") or {}).get("variant") or {}
        inventory_item_id = variant.get("inventory_item_id")
        if not inventory_item_id:
            logger.info("RESERVATIONS | Shopify sync skipped: no inventory_item_id for variant %s",
                        variant_id)
            return False

        locations = (client.get("locations.json") or {}).get("locations") or []
        if not locations:
            logger.info("RESERVATIONS | Shopify sync skipped: no locations for variant %s",
                        variant_id)
            return False
        location_id = locations[0].get("id")
        if not location_id:
            logger.info("RESERVATIONS | Shopify sync skipped: first location has no id")
            return False

        resp = client.post(
            "inventory_levels/adjust.json",
            {
                "location_id": location_id,
                "inventory_item_id": inventory_item_id,
                "available_adjustment": int(delta),
            },
        )
        if resp is None:
            logger.warning("RESERVATIONS | Shopify adjust returned no response for variant %s",
                           variant_id)
            return False

        logger.info("RESERVATIONS | Shopify inventory adjusted by %d for variant %s",
                    delta, variant_id)
        return True
    except Exception as exc:  # noqa: BLE001 - sync must never break reservations
        logger.warning("RESERVATIONS | Shopify adjust failed for variant %s: %s", variant_id, exc)
        return False
