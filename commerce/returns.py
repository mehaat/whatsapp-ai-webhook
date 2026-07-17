"""
commerce/returns.py
--------------------
The v7.0 Returns / Refund / Exchange (RMA) workflow.

A :class:`~database.models.ReturnRequest` is minted against an existing order
with a human-facing RMA number (e.g. ``RMA-2026-000001``) via
:func:`commerce.numbering.next_number`. The request then moves through a small
status machine — ``requested -> approved | rejected -> completed`` — and, on
completion of a refund/return, flips the underlying order to ``refunded`` via
:class:`commerce.service.OrderService`.

Every public function is defensive: it returns a plain, serializable ``dict``
(or ``None`` / ``[]`` / ``0``) and never raises, so callers — the WhatsApp
webhook, the admin dashboard, background jobs — can rely on it unconditionally.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from database.db import session_scope
from database.models import Order, ReturnRequest
from commerce.numbering import next_number
from commerce.service import order_service
from utils.logging import logger

# Canonical RMA workflow statuses.
RETURN_STATUSES = ("requested", "approved", "rejected", "completed")
# Kinds that, when completed, refund the underlying order.
_REFUNDING_KINDS = ("refund", "return")


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


def _to_dict(r: ReturnRequest) -> Dict[str, Any]:
    return {
        "id": r.id,
        "rma_number": r.rma_number,
        "order_id": r.order_id,
        "wa_number": r.wa_number,
        "kind": r.kind,
        "reason": r.reason,
        "status": r.status,
        "refund_amount": _f(r.refund_amount),
        "resolution": r.resolution,
        "created_at": _iso(r.created_at),
        "updated_at": _iso(r.updated_at),
    }


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------

def create_return(
    order_id: int,
    *,
    kind: str = "return",
    reason: Optional[str] = None,
    wa_number: Optional[str] = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """Open a new RMA against an order. Never raises.

    Args:
        order_id: The order the request is filed against.
        kind: ``return`` | ``refund`` | ``exchange`` (defaults to ``return``).
        reason: Free-text customer reason.
        wa_number: Customer WhatsApp number; inferred from the order if omitted.
        actor: Who is filing the request (for auditing).

    Returns:
        The created return as a dict, or ``{"error": ...}`` on failure.
    """
    kind = (kind or "return").strip().lower()
    try:
        with session_scope() as session:
            order = session.get(Order, order_id)
            if order is None:
                return {"error": "order_not_found", "order_id": order_id}
            resolved_wa = wa_number or order.wa_number
            rma_number = next_number(session, "rma", "RMA")
            req = ReturnRequest(
                rma_number=rma_number,
                order_id=order_id,
                wa_number=resolved_wa,
                kind=kind,
                reason=reason,
                status="requested",
                refund_amount=Decimal("0"),
            )
            session.add(req)
            session.flush()
            order_service._audit(
                session, actor, "return.create", "return", str(req.id),
                f"{rma_number} kind={kind} order={order_id}",
            )
            result = _to_dict(req)
        logger.info("COMMERCE | Return %s opened for order #%s (kind=%s)",
                    result["rma_number"], order_id, kind)
        return result
    except Exception as exc:  # noqa: BLE001 - never break the caller
        logger.error("COMMERCE | create_return failed for order #%s: %s", order_id, exc)
        return {"error": "create_failed", "detail": str(exc)}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def list_returns(status: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Return RMAs newest-first, optionally filtered by status. Never raises."""
    try:
        with session_scope() as session:
            q = session.query(ReturnRequest)
            if status:
                q = q.filter(ReturnRequest.status == status)
            q = q.order_by(ReturnRequest.created_at.desc(), ReturnRequest.id.desc())
            q = q.limit(limit).offset(offset)
            return [_to_dict(r) for r in q.all()]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | list_returns failed: %s", exc)
        return []


def count_returns(**filters: Any) -> int:
    """Count RMAs, optionally filtered by ``status``. Never raises."""
    status = filters.get("status")
    try:
        with session_scope() as session:
            q = session.query(ReturnRequest)
            if status:
                q = q.filter(ReturnRequest.status == status)
            return q.count()
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | count_returns failed: %s", exc)
        return 0


def get_return(rid_or_rma: Any) -> Optional[Dict[str, Any]]:
    """Fetch one RMA by numeric id or RMA number. Never raises."""
    try:
        with session_scope() as session:
            req = _resolve(session, rid_or_rma)
            return _to_dict(req) if req is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | get_return failed for %r: %s", rid_or_rma, exc)
        return None


def latest_return_for(wa_number: str) -> Optional[Dict[str, Any]]:
    """Return the most recent RMA for a customer, or ``None``. Never raises."""
    try:
        with session_scope() as session:
            req = (
                session.query(ReturnRequest)
                .filter(ReturnRequest.wa_number == wa_number)
                .order_by(ReturnRequest.created_at.desc(), ReturnRequest.id.desc())
                .first()
            )
            return _to_dict(req) if req is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | latest_return_for failed for %s: %s", wa_number, exc)
        return None


# --------------------------------------------------------------------------
# State changes
# --------------------------------------------------------------------------

def update_return_status(
    rid: Any,
    status: str,
    *,
    refund_amount: Optional[Any] = None,
    resolution: Optional[str] = None,
    actor: str = "admin",
) -> Dict[str, Any]:
    """Advance an RMA's status and apply order-level side effects. Never raises.

    Valid statuses: ``requested`` | ``approved`` | ``rejected`` | ``completed``.

    When a ``refund``/``return`` RMA reaches ``completed`` the underlying order
    is flipped to ``refunded`` (which also marks its payment as refunded) via
    :meth:`commerce.service.OrderService.set_status`.

    Args:
        rid: The RMA numeric id or RMA number.
        status: The target status.
        refund_amount: Optional refund amount to record.
        resolution: Optional free-text resolution note.
        actor: Who is performing the change (for auditing).

    Returns:
        The updated return as a dict, or ``{"error": ...}`` on failure.
    """
    status = (status or "").strip().lower()
    if status not in RETURN_STATUSES:
        return {"error": "invalid_status", "status": status}

    order_id_to_refund: Optional[int] = None
    kind = ""
    try:
        with session_scope() as session:
            req = _resolve(session, rid)
            if req is None:
                return {"error": "return_not_found", "rid": rid}
            req.status = status
            if refund_amount is not None:
                req.refund_amount = _to_decimal(refund_amount)
            if resolution is not None:
                req.resolution = resolution
            kind = (req.kind or "").lower()
            if status == "completed" and kind in _REFUNDING_KINDS:
                order_id_to_refund = req.order_id
            session.flush()
            order_service._audit(
                session, actor, "return.status", "return", str(req.id),
                f"{req.rma_number} -> {status}",
            )
            result = _to_dict(req)

        # Apply order-level side effects AFTER the RMA transaction commits so a
        # failure there can never corrupt the RMA record.
        if order_id_to_refund is not None:
            try:
                order_service.set_payment_status(order_id_to_refund, "refunded", actor=actor)
                order_service.set_status(order_id_to_refund, "refunded", actor=actor)
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.error("COMMERCE | refund side effect failed for order #%s: %s",
                             order_id_to_refund, exc)

        logger.info("COMMERCE | Return %s -> %s", result["rma_number"], status)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | update_return_status failed for %r: %s", rid, exc)
        return {"error": "update_failed", "detail": str(exc)}


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

def _resolve(session, rid_or_rma: Any) -> Optional[ReturnRequest]:
    """Resolve an RMA by numeric id first, then by RMA number string."""
    if isinstance(rid_or_rma, int) or (isinstance(rid_or_rma, str) and rid_or_rma.isdigit()):
        req = session.get(ReturnRequest, int(rid_or_rma))
        if req is not None:
            return req
    if isinstance(rid_or_rma, str):
        return session.query(ReturnRequest).filter_by(rma_number=rid_or_rma).first()
    return None


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
