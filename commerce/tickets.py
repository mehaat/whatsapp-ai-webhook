"""
commerce/tickets.py
--------------------
The v7.0 customer Support-Ticket workflow.

A :class:`~database.models.SupportTicket` is minted with a human-facing ticket
number (e.g. ``TKT-2026-000001``) via :func:`commerce.numbering.next_number`.
Each ticket owns a thread of :class:`~database.models.TicketMessage` rows and
moves through a small status machine — ``open -> pending -> resolved -> closed``.

Every public function is defensive: it returns a plain, serializable ``dict``
(or ``None`` / ``[]`` / ``0``) and never raises, so callers — the WhatsApp
webhook (a "human agent" hand-off), the admin dashboard, background jobs — can
rely on it unconditionally.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from database.db import session_scope
from database.models import SupportTicket, TicketMessage
from commerce.numbering import next_number
from commerce.service import order_service
from utils.logging import logger

# Canonical ticket workflow statuses.
TICKET_STATUSES = ("open", "pending", "resolved", "closed")
# Canonical ticket priorities.
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _ticket_to_dict(t: SupportTicket, messages=None) -> Dict[str, Any]:
    data = {
        "id": t.id,
        "ticket_number": t.ticket_number,
        "wa_number": t.wa_number,
        "subject": t.subject,
        "status": t.status,
        "priority": t.priority,
        "assigned_to": t.assigned_to,
        "order_id": t.order_id,
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }
    if messages is not None:
        data["messages"] = [_message_to_dict(m) for m in messages]
    return data


def _message_to_dict(m: TicketMessage) -> Dict[str, Any]:
    return {
        "id": m.id,
        "ticket_id": m.ticket_id,
        "author": m.author,
        "body": m.body,
        "created_at": _iso(m.created_at),
    }


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------

def create_ticket(
    subject: str,
    *,
    wa_number: Optional[str] = None,
    priority: str = "normal",
    order_id: Optional[int] = None,
    body: Optional[str] = None,
    author: str = "customer",
) -> Dict[str, Any]:
    """Open a new support ticket. Never raises.

    Mints a ``TKT-YYYY-NNNNNN`` number, creates the :class:`SupportTicket` in the
    ``open`` state and, when ``body`` is provided, records the first
    :class:`TicketMessage` in its thread.

    Args:
        subject: Short ticket subject line.
        wa_number: Customer WhatsApp number (optional).
        priority: One of ``low`` | ``normal`` | ``high`` | ``urgent``.
        order_id: Optionally link the ticket to an order.
        body: Optional first-message body (creates a thread message).
        author: Who opened the ticket (defaults to ``customer``).

    Returns:
        The created ticket as a dict (with ``messages``), or ``{"error": ...}``.
    """
    subject = (subject or "").strip() or "(no subject)"
    priority = (priority or "normal").strip().lower()
    if priority not in TICKET_PRIORITIES:
        priority = "normal"
    try:
        with session_scope() as session:
            ticket_number = next_number(session, "ticket", "TKT")
            ticket = SupportTicket(
                ticket_number=ticket_number,
                wa_number=wa_number,
                subject=subject[:255],
                status="open",
                priority=priority,
                order_id=order_id,
            )
            session.add(ticket)
            session.flush()
            messages: List[TicketMessage] = []
            if body and body.strip():
                msg = TicketMessage(
                    ticket_id=ticket.id, author=author or "customer", body=body.strip()
                )
                session.add(msg)
                session.flush()
                messages = [msg]
            order_service._audit(
                session, author or "customer", "ticket.create", "ticket",
                str(ticket.id), f"{ticket_number} priority={priority}",
            )
            result = _ticket_to_dict(ticket, messages)
        logger.info("COMMERCE | Ticket %s opened (priority=%s wa=%s)",
                    result["ticket_number"], priority, wa_number)
        return result
    except Exception as exc:  # noqa: BLE001 - never break the caller
        logger.error("COMMERCE | create_ticket failed: %s", exc)
        return {"error": "create_failed", "detail": str(exc)}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def list_tickets(status: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Return tickets newest-first, optionally filtered by status. Never raises."""
    try:
        with session_scope() as session:
            q = session.query(SupportTicket)
            if status:
                q = q.filter(SupportTicket.status == status)
            q = q.order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
            q = q.limit(limit).offset(offset)
            return [_ticket_to_dict(t) for t in q.all()]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | list_tickets failed: %s", exc)
        return []


def count_tickets(**filters: Any) -> int:
    """Count tickets, optionally filtered by ``status``. Never raises."""
    status = filters.get("status")
    try:
        with session_scope() as session:
            q = session.query(SupportTicket)
            if status:
                q = q.filter(SupportTicket.status == status)
            return q.count()
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | count_tickets failed: %s", exc)
        return 0


def get_ticket(tid: Any) -> Optional[Dict[str, Any]]:
    """Fetch one ticket (with its message thread) by id or ticket number."""
    try:
        with session_scope() as session:
            ticket = _resolve(session, tid)
            if ticket is None:
                return None
            messages = (
                session.query(TicketMessage)
                .filter_by(ticket_id=ticket.id)
                .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
                .all()
            )
            return _ticket_to_dict(ticket, messages)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | get_ticket failed for %r: %s", tid, exc)
        return None


def latest_ticket_for(wa_number: str) -> Optional[Dict[str, Any]]:
    """Return the most recent ticket for a customer, or ``None``. Never raises."""
    try:
        with session_scope() as session:
            ticket = (
                session.query(SupportTicket)
                .filter(SupportTicket.wa_number == wa_number)
                .order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
                .first()
            )
            return _ticket_to_dict(ticket) if ticket is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | latest_ticket_for failed for %s: %s", wa_number, exc)
        return None


# --------------------------------------------------------------------------
# State changes
# --------------------------------------------------------------------------

def add_message(tid: Any, body: str, author: str) -> Dict[str, Any]:
    """Append a message to a ticket thread. Never raises.

    Adding an agent reply to a ``resolved``/``closed`` ticket re-opens it as
    ``pending`` so it resurfaces for the customer.

    Returns:
        The created message as a dict, or ``{"error": ...}`` on failure.
    """
    if not body or not body.strip():
        return {"error": "empty_body"}
    try:
        with session_scope() as session:
            ticket = _resolve(session, tid)
            if ticket is None:
                return {"error": "ticket_not_found", "tid": tid}
            msg = TicketMessage(
                ticket_id=ticket.id, author=(author or "agent"), body=body.strip()
            )
            session.add(msg)
            if ticket.status in ("resolved", "closed"):
                ticket.status = "pending"
            session.flush()
            order_service._audit(
                session, author or "agent", "ticket.message", "ticket",
                str(ticket.id), f"{ticket.ticket_number} +message",
            )
            return _message_to_dict(msg)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | add_message failed for %r: %s", tid, exc)
        return {"error": "message_failed", "detail": str(exc)}


def set_status(tid: Any, status: str, actor: str) -> Dict[str, Any]:
    """Change a ticket's status. Never raises.

    Valid statuses: ``open`` | ``pending`` | ``resolved`` | ``closed``.

    Returns:
        The updated ticket as a dict, or ``{"error": ...}`` on failure.
    """
    status = (status or "").strip().lower()
    if status not in TICKET_STATUSES:
        return {"error": "invalid_status", "status": status}
    try:
        with session_scope() as session:
            ticket = _resolve(session, tid)
            if ticket is None:
                return {"error": "ticket_not_found", "tid": tid}
            ticket.status = status
            session.flush()
            order_service._audit(
                session, actor or "admin", "ticket.status", "ticket",
                str(ticket.id), f"{ticket.ticket_number} -> {status}",
            )
            result = _ticket_to_dict(ticket)
        logger.info("COMMERCE | Ticket %s -> %s", result["ticket_number"], status)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | set_status failed for %r: %s", tid, exc)
        return {"error": "update_failed", "detail": str(exc)}


def assign(tid: Any, username: str, actor: str) -> Dict[str, Any]:
    """Assign (or unassign, when ``username`` is blank) a ticket. Never raises.

    Returns:
        The updated ticket as a dict, or ``{"error": ...}`` on failure.
    """
    username = (username or "").strip() or None
    try:
        with session_scope() as session:
            ticket = _resolve(session, tid)
            if ticket is None:
                return {"error": "ticket_not_found", "tid": tid}
            ticket.assigned_to = username
            session.flush()
            order_service._audit(
                session, actor or "admin", "ticket.assign", "ticket",
                str(ticket.id), f"{ticket.ticket_number} assigned_to={username or '—'}",
            )
            result = _ticket_to_dict(ticket)
        logger.info("COMMERCE | Ticket %s assigned to %s", result["ticket_number"], username)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | assign failed for %r: %s", tid, exc)
        return {"error": "assign_failed", "detail": str(exc)}


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

def _resolve(session, tid: Any) -> Optional[SupportTicket]:
    """Resolve a ticket by numeric id first, then by ticket number string."""
    if isinstance(tid, int) or (isinstance(tid, str) and tid.isdigit()):
        ticket = session.get(SupportTicket, int(tid))
        if ticket is not None:
            return ticket
    if isinstance(tid, str):
        return session.query(SupportTicket).filter_by(ticket_number=tid).first()
    return None
