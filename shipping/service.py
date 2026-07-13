"""
shipping/service.py
--------------------
Orchestration for the v7.0 fulfilment & shipping system — the single entry
point the admin UI and background jobs use to create shipments, refresh
tracking, schedule pickups and list/read shipments.

Every public function is defensive: it returns a plain ``dict`` result carrying
``ok`` and (on failure) ``error`` rather than raising, so a courier outage or a
bad credential never 500s the admin dashboard or crashes a job. Persistence
goes through :func:`database.db.session_scope`; results are always detached
dicts (never live ORM instances).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.logging import logger

from shipping.base import ShipmentResult
from shipping.factory import get_provider


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _shipment_to_dict(s) -> Dict[str, Any]:
    """Serialize a :class:`Shipment` ORM row to a detached dict."""
    return {
        "id": s.id,
        "order_id": s.order_id,
        "provider": s.provider,
        "awb": s.awb,
        "courier_name": s.courier_name,
        "label_url": s.label_url,
        "tracking_url": s.tracking_url,
        "status": s.status,
        "provider_shipment_id": s.provider_shipment_id,
        "pickup_scheduled_at": _iso(s.pickup_scheduled_at),
        "raw": s.raw,
        "created_at": _iso(s.created_at),
        "updated_at": _iso(s.updated_at),
    }


def _dump_raw(raw: Any) -> Optional[str]:
    """Serialize a provider raw payload to a JSON string (best-effort)."""
    if raw is None:
        return None
    try:
        return json.dumps(raw)[:8000]
    except Exception:  # noqa: BLE001
        return str(raw)[:8000]


def create_shipment_for_order(
    order_id: int, provider_name: Optional[str] = None
) -> Dict[str, Any]:
    """Create a shipment for an order and mark the order ``shipped``.

    Steps:
        1. Load the order via ``order_service.get_order``.
        2. Resolve the courier via :func:`shipping.factory.get_provider`.
        3. Call ``create_shipment``; on success persist a ``Shipment`` row.
        4. Move the order to ``shipped`` (recording courier + AWB).

    Never raises: any failure is captured and returned as ``{"ok": False,
    "error": ...}``.

    Args:
        order_id: The local order id.
        provider_name: Optional explicit courier name (else config default).

    Returns:
        ``{"ok": bool, "shipment": dict|None, "error": str|None}``.
    """
    try:
        from commerce.service import order_service

        order = order_service.get_order(order_id=order_id, include_items=True)
        if order is None:
            return {"ok": False, "shipment": None, "error": "order_not_found"}

        provider = get_provider(provider_name)
        try:
            result: ShipmentResult = provider.create_shipment(order)
        except Exception as exc:  # noqa: BLE001 - courier/credential failure
            logger.error("SHIPPING | create_shipment failed for order %s: %s",
                         order_id, exc)
            order_service.audit(
                actor="shipping", action="shipment.error", entity="order",
                entity_id=str(order_id), detail=f"{provider.name}: {exc}",
            )
            return {"ok": False, "shipment": None, "error": str(exc)}

        if not result.ok:
            return {"ok": False, "shipment": None,
                    "error": "provider_declined", "provider": provider.name}

        shipment_dict = _persist_shipment(order_id, result)

        # Move the order to 'shipped', recording courier + AWB on the order.
        try:
            order_service.set_status(
                order_id, "shipped", actor="shipping",
                courier=result.courier_name or result.provider,
                tracking_number=result.awb,
                note=f"Shipment created via {result.provider}"
                     + (f" (AWB {result.awb})" if result.awb else ""),
            )
        except Exception as exc:  # noqa: BLE001 - shipment row exists; don't lose it
            logger.error("SHIPPING | set_status(shipped) failed for order %s: %s",
                         order_id, exc)

        order_service.audit(
            actor="shipping", action="shipment.create", entity="shipment",
            entity_id=str(shipment_dict.get("id")),
            detail=f"{result.provider} awb={result.awb} order={order_id}",
        )
        logger.info("SHIPPING | shipment #%s created for order %s (awb=%s)",
                    shipment_dict.get("id"), order_id, result.awb)
        return {"ok": True, "shipment": shipment_dict, "error": None}

    except Exception as exc:  # noqa: BLE001 - orchestration must never raise
        logger.error("SHIPPING | create_shipment_for_order crashed for %s: %s",
                     order_id, exc)
        return {"ok": False, "shipment": None, "error": str(exc)}


def _persist_shipment(order_id: int, result: ShipmentResult) -> Dict[str, Any]:
    """Insert a :class:`Shipment` row from a provider result."""
    from database.db import session_scope
    from database.models import Shipment

    with session_scope() as session:
        row = Shipment(
            order_id=order_id,
            provider=result.provider,
            awb=result.awb,
            courier_name=result.courier_name,
            label_url=result.label_url,
            tracking_url=result.tracking_url,
            status=result.status or "created",
            provider_shipment_id=result.provider_shipment_id,
            raw=_dump_raw(result.raw),
        )
        session.add(row)
        session.flush()
        return _shipment_to_dict(row)


def track_shipment(
    order_id: Optional[int] = None, awb: Optional[str] = None
) -> Dict[str, Any]:
    """Refresh tracking for a shipment (by order id or AWB).

    Loads the shipment row, queries its courier, updates the row's status and
    returns the tracking result. Never raises.

    Args:
        order_id: Resolve the latest shipment for this order.
        awb: Resolve the shipment directly by AWB.

    Returns:
        ``{"ok": bool, "status": str, "checkpoints": list, "shipment": dict|None,
        "error": str|None}``.
    """
    try:
        from database.db import session_scope
        from database.models import Shipment

        with session_scope() as session:
            row = None
            if awb:
                row = session.query(Shipment).filter_by(awb=awb) \
                    .order_by(Shipment.id.desc()).first()
            elif order_id is not None:
                row = session.query(Shipment).filter_by(order_id=order_id) \
                    .order_by(Shipment.id.desc()).first()
            if row is None:
                return {"ok": False, "status": "unknown", "checkpoints": [],
                        "shipment": None, "error": "shipment_not_found"}
            provider_name = row.provider
            row_awb = row.awb
            row_order_id = row.order_id
            shipment_id = row.id

        if not row_awb:
            return {"ok": False, "status": "unknown", "checkpoints": [],
                    "shipment": None, "error": "no_awb"}

        provider = get_provider(provider_name)
        tracking = provider.track(row_awb)

        # Persist the refreshed status back onto the shipment row.
        with session_scope() as session:
            row = session.get(Shipment, shipment_id)
            if row is not None and tracking.ok and tracking.status:
                row.status = tracking.status
                shipment_dict = _shipment_to_dict(row)
            else:
                shipment_dict = _shipment_to_dict(row) if row is not None else None

        # Mirror courier-side delivery onto the order lifecycle (best-effort).
        if tracking.ok and tracking.status in {"delivered", "out_for_delivery"}:
            try:
                from commerce.service import order_service

                order_service.set_status(
                    row_order_id, tracking.status, actor="shipping",
                    note=f"Courier update: {tracking.status}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("SHIPPING | order status mirror skipped: %s", exc)

        return {
            "ok": tracking.ok,
            "status": tracking.status,
            "checkpoints": tracking.checkpoints,
            "shipment": shipment_dict,
            "error": None if tracking.ok else "track_failed",
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("SHIPPING | track_shipment crashed: %s", exc)
        return {"ok": False, "status": "unknown", "checkpoints": [],
                "shipment": None, "error": str(exc)}


def schedule_pickup(shipment_id: int) -> Dict[str, Any]:
    """Schedule a courier pickup for a persisted shipment. Never raises.

    Args:
        shipment_id: The shipment row id.

    Returns:
        ``{"ok": bool, "shipment": dict|None, "error": str|None}``.
    """
    try:
        from database.db import session_scope
        from database.models import Shipment

        with session_scope() as session:
            row = session.get(Shipment, shipment_id)
            if row is None:
                return {"ok": False, "shipment": None, "error": "shipment_not_found"}
            shipment_dict = _shipment_to_dict(row)
            provider_name = row.provider

        provider = get_provider(provider_name)
        try:
            ok = provider.schedule_pickup(shipment_dict)
        except Exception as exc:  # noqa: BLE001
            logger.error("SHIPPING | schedule_pickup failed for #%s: %s",
                         shipment_id, exc)
            return {"ok": False, "shipment": shipment_dict, "error": str(exc)}

        if ok:
            with session_scope() as session:
                row = session.get(Shipment, shipment_id)
                if row is not None:
                    row.pickup_scheduled_at = datetime.now(timezone.utc)
                    shipment_dict = _shipment_to_dict(row)
        return {"ok": bool(ok), "shipment": shipment_dict,
                "error": None if ok else "pickup_declined"}
    except Exception as exc:  # noqa: BLE001
        logger.error("SHIPPING | schedule_pickup crashed for #%s: %s",
                     shipment_id, exc)
        return {"ok": False, "shipment": None, "error": str(exc)}


def list_shipments(
    status: Optional[str] = None, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    """Return shipments (newest first), optionally filtered by status."""
    try:
        from database.db import session_scope
        from database.models import Shipment

        with session_scope() as session:
            q = session.query(Shipment)
            if status:
                q = q.filter(Shipment.status == status)
            q = q.order_by(Shipment.id.desc()).limit(limit).offset(offset)
            return [_shipment_to_dict(s) for s in q.all()]
    except Exception as exc:  # noqa: BLE001
        logger.error("SHIPPING | list_shipments failed: %s", exc)
        return []


def get_shipment(shipment_id: int) -> Optional[Dict[str, Any]]:
    """Return a single shipment by id, or ``None`` if not found."""
    try:
        from database.db import session_scope
        from database.models import Shipment

        with session_scope() as session:
            row = session.get(Shipment, shipment_id)
            return _shipment_to_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("SHIPPING | get_shipment failed: %s", exc)
        return None
