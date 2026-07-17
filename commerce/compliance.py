"""
commerce/compliance.py
-----------------------
v8.0 enterprise compliance service: PII access logging and GDPR/DPDP
data-subject rights (export, erasure) plus a retention purge.

Design principles:
    * Every public function is defensive — compliance tooling must never crash
      the caller, so failures are logged and returned as ``{"ok": False, ...}``
      dicts (never raised).
    * "Erasure" pseudonymizes/redacts personal data while *retaining* financial
      records (orders, payments) for accounting/tax obligations: the customer's
      name is blanked but the order and its money trail survive.
    * All database access flows through :func:`database.db.session_scope`.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import config
from database.db import session_scope
from database.models import (
    Conversation,
    CrmProfile,
    Customer,
    CustomerNote,
    DataRequest,
    NotificationLog,
    Order,
    OrderItem,
    Payment,
    PiiAccessLog,
    ReturnRequest,
    SupportTicket,
    WishlistItem,
)
from utils.logging import logger

_REDACTED = "[erased]"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _sanitize_wa(wa_number: str) -> str:
    """Return a filesystem-safe token derived from a WhatsApp number."""
    return re.sub(r"[^A-Za-z0-9_-]", "", (wa_number or "")) or "unknown"


def _row_to_dict(obj) -> Dict[str, Any]:
    """Serialize a SQLAlchemy model instance to a JSON-friendly dict."""
    data: Dict[str, Any] = {}
    for col in obj.__table__.columns:  # type: ignore[attr-defined]
        value = getattr(obj, col.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        else:
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = str(value)
        data[col.name] = value
    return data


# --------------------------------------------------------------------------
# PII access logging
# --------------------------------------------------------------------------

def log_pii_access(actor, wa_number, action, *, ip=None) -> None:
    """Record an access to a customer's personal data (never raises).

    Args:
        actor: Who accessed the data.
        wa_number: The data subject's WhatsApp number.
        action: One of ``view`` | ``export`` | ``erase``.
        ip: Optional client IP.
    """
    try:
        with session_scope() as session:
            session.add(
                PiiAccessLog(
                    actor=(actor or "system")[:128],
                    subject_wa_number=(wa_number or "")[:32],
                    action=(action or "view")[:64],
                    ip=ip,
                )
            )
    except Exception as exc:  # noqa: BLE001 - logging must never break the op
        logger.debug("COMPLIANCE | pii access log failed: %s", exc)


# --------------------------------------------------------------------------
# Data-subject export (GDPR Art. 15 / 20)
# --------------------------------------------------------------------------

def data_subject_export(wa_number, *, actor="admin", ip=None) -> dict:
    """Collect every stored record for ``wa_number`` into a JSON file.

    Writes ``{compliance_export_dir}/export_{wa}_{request_id}.json`` containing
    the customer profile, CRM enrichment, conversations, orders + items,
    payments, notes, wishlist, returns, tickets and notifications. Records a
    completed :class:`~database.models.DataRequest` and a PII-access log entry.

    Returns:
        ``{"ok", "path", "request_id", "summary": {counts}}`` on success, or
        ``{"ok": False, "error": ...}`` on failure.
    """
    try:
        with session_scope() as session:
            customer = (
                session.query(Customer).filter_by(wa_number=wa_number).first()
            )
            crm = session.get(CrmProfile, wa_number)
            conversations = (
                session.query(Conversation)
                .filter_by(wa_number=wa_number)
                .order_by(Conversation.id.asc())
                .all()
            )
            orders = (
                session.query(Order)
                .filter_by(wa_number=wa_number)
                .order_by(Order.id.asc())
                .all()
            )
            order_ids = [o.id for o in orders]
            items = (
                session.query(OrderItem)
                .filter(OrderItem.order_id.in_(order_ids))
                .all()
                if order_ids
                else []
            )
            payments = (
                session.query(Payment)
                .filter(Payment.order_id.in_(order_ids))
                .all()
                if order_ids
                else []
            )
            notes = (
                session.query(CustomerNote).filter_by(wa_number=wa_number).all()
            )
            wishlist = (
                session.query(WishlistItem).filter_by(wa_number=wa_number).all()
            )
            returns = (
                session.query(ReturnRequest).filter_by(wa_number=wa_number).all()
            )
            tickets = (
                session.query(SupportTicket).filter_by(wa_number=wa_number).all()
            )
            notifications = (
                session.query(NotificationLog)
                .filter_by(wa_number=wa_number)
                .all()
            )

            payload: Dict[str, Any] = {
                "subject_wa_number": wa_number,
                "generated_at": _utcnow().isoformat(),
                "customer": _row_to_dict(customer) if customer else None,
                "crm_profile": _row_to_dict(crm) if crm else None,
                "conversations": [_row_to_dict(c) for c in conversations],
                "orders": [_row_to_dict(o) for o in orders],
                "order_items": [_row_to_dict(i) for i in items],
                "payments": [_row_to_dict(p) for p in payments],
                "notes": [_row_to_dict(n) for n in notes],
                "wishlist": [_row_to_dict(w) for w in wishlist],
                "returns": [_row_to_dict(r) for r in returns],
                "tickets": [_row_to_dict(t) for t in tickets],
                "notifications": [_row_to_dict(n) for n in notifications],
            }
            summary = {
                "conversations": len(conversations),
                "orders": len(orders),
                "order_items": len(items),
                "payments": len(payments),
                "notes": len(notes),
                "wishlist": len(wishlist),
                "returns": len(returns),
                "tickets": len(tickets),
                "notifications": len(notifications),
            }

            req = DataRequest(
                kind="export",
                subject_wa_number=wa_number,
                status="pending",
                requested_by=actor,
            )
            session.add(req)
            session.flush()  # assign req.id

            export_dir = config.compliance_export_dir or "exports"
            os.makedirs(export_dir, exist_ok=True)
            path = os.path.join(
                export_dir, f"export_{_sanitize_wa(wa_number)}_{req.id}.json"
            )
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)

            req.result_path = path
            req.status = "completed"
            req.detail = json.dumps(summary)
            req.completed_at = _utcnow()
            request_id = req.id

        log_pii_access(actor, wa_number, "export", ip=ip)
        logger.info("COMPLIANCE | export completed for %s -> %s", wa_number, path)
        return {"ok": True, "path": path, "request_id": request_id, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        logger.error("COMPLIANCE | export failed for %s: %s", wa_number, exc)
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------
# Data-subject erasure (GDPR Art. 17 — with financial-record retention)
# --------------------------------------------------------------------------

def erase_customer(wa_number, *, actor="admin", ip=None) -> dict:
    """Pseudonymize/redact a customer's PII while keeping financial records.

    Blanks the customer's display names, note bodies and conversation text,
    withdraws marketing consent, and blanks the customer name on orders — but
    retains the orders/payments themselves for accounting. Records a completed
    erasure :class:`~database.models.DataRequest` and a PII-access log entry.

    Returns:
        ``{"ok", "erased": {counts}}`` on success, or ``{"ok": False, "error"}``.
    """
    try:
        with session_scope() as session:
            erased: Dict[str, int] = {}

            customer = (
                session.query(Customer).filter_by(wa_number=wa_number).first()
            )
            if customer is not None:
                customer.profile_name = _REDACTED
                erased["customer"] = 1

            crm = session.get(CrmProfile, wa_number)
            if crm is not None:
                crm.display_name = _REDACTED
                crm.marketing_consent = False
                erased["crm_profile"] = 1

            notes = (
                session.query(CustomerNote).filter_by(wa_number=wa_number).all()
            )
            for note in notes:
                note.note = _REDACTED
            erased["notes"] = len(notes)

            conversations = (
                session.query(Conversation).filter_by(wa_number=wa_number).all()
            )
            for convo in conversations:
                convo.text = _REDACTED
            erased["conversations"] = len(conversations)

            orders = (
                session.query(Order).filter_by(wa_number=wa_number).all()
            )
            for order in orders:
                order.customer_name = _REDACTED
            erased["orders_redacted"] = len(orders)

            session.add(
                DataRequest(
                    kind="erasure",
                    subject_wa_number=wa_number,
                    status="completed",
                    requested_by=actor,
                    detail=json.dumps(erased),
                    completed_at=_utcnow(),
                )
            )

        log_pii_access(actor, wa_number, "erase", ip=ip)
        logger.info("COMPLIANCE | erasure completed for %s: %s", wa_number, erased)
        return {"ok": True, "erased": erased}
    except Exception as exc:  # noqa: BLE001
        logger.error("COMPLIANCE | erasure failed for %s: %s", wa_number, exc)
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------
# Retention purge
# --------------------------------------------------------------------------

def retention_purge(days: Optional[int] = None) -> dict:
    """Redact conversation text older than the retention window.

    When ``days`` (falling back to :data:`config.data_retention_days`) is
    ``<= 0`` retention is disabled and nothing is purged. Orders and payments
    are never hard-deleted (accounting retention); only free-text conversation
    bodies are redacted.

    Returns:
        ``{"ok", "purged", "skipped"?}``.
    """
    try:
        window = days if days is not None else config.data_retention_days
        try:
            window = int(window or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            return {"ok": True, "purged": 0, "skipped": True}

        cutoff = _utcnow() - timedelta(days=window)
        with session_scope() as session:
            stale = (
                session.query(Conversation)
                .filter(Conversation.created_at < cutoff)
                .all()
            )
            for convo in stale:
                convo.text = "[purged]"
            purged = len(stale)

        logger.info("COMPLIANCE | retention purge redacted %d conversations", purged)
        return {"ok": True, "purged": purged}
    except Exception as exc:  # noqa: BLE001
        logger.error("COMPLIANCE | retention purge failed: %s", exc)
        return {"ok": False, "purged": 0, "error": str(exc)}


# --------------------------------------------------------------------------
# Read helpers (for the admin dashboard)
# --------------------------------------------------------------------------

def list_data_requests(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent data-subject requests (newest first)."""
    try:
        with session_scope() as session:
            rows = (
                session.query(DataRequest)
                .order_by(DataRequest.id.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "subject_wa_number": r.subject_wa_number,
                    "status": r.status,
                    "requested_by": r.requested_by,
                    "result_path": r.result_path,
                    "detail": r.detail,
                    "created_at": _iso(r.created_at),
                    "completed_at": _iso(r.completed_at),
                }
                for r in rows
            ]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMPLIANCE | list_data_requests failed: %s", exc)
        return []


def list_pii_access(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent PII-access log entries (newest first)."""
    try:
        with session_scope() as session:
            rows = (
                session.query(PiiAccessLog)
                .order_by(PiiAccessLog.id.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "actor": r.actor,
                    "subject_wa_number": r.subject_wa_number,
                    "action": r.action,
                    "ip": r.ip,
                    "created_at": _iso(r.created_at),
                }
                for r in rows
            ]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMPLIANCE | list_pii_access failed: %s", exc)
        return []
