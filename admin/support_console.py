"""
admin/support_console.py
------------------------
Service layer for the v10.2 real-time WhatsApp Support Console.

Pure data/business logic (no Flask): per-conversation AI toggle, assignment,
internal notes, admin message recording, delivery-status tracking, a merged
customer↔bot↔admin timeline, live statistics and a customer profile. The HTTP
blueprint (``admin/support_routes.py``) is a thin layer over these functions.

Everything goes through the ONE shared SQLAlchemy engine, so it works on SQLite
and PostgreSQL alike. Every function is written to be safe to call from request
handlers and from the WhatsApp webhook path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from database.db import session_scope
from database.models_support import (
    AdminMessage,
    ConversationAssignment,
    ConversationSettings,
    InternalNote,
    MessageStatus,
)
from database.models_admin import DashConversation, DashCustomer, DashMessage, DashOrder
from utils.logging import logger


# --------------------------------------------------------------------------- #
# Time helpers (ISO strings, matching the existing dashboard tables)
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(actor: str, action: str, entity_id: str = "", detail: str = "", ip: str = "") -> None:
    """Write a tamper-evident admin audit row (best-effort; never raises).

    Reuses the project's ``audit_logs`` table + hash chain so console actions
    appear alongside the rest of the admin audit trail.
    """
    try:
        from database.models import AuditLog

        with session_scope() as session:
            row = AuditLog(
                actor=actor or "admin", action=action, entity="support_console",
                entity_id=(entity_id or None), detail=(detail or "")[:2000], ip=ip or None,
            )
            session.add(row)
            try:
                from commerce.audit_chain import apply_chain

                session.flush()
                apply_chain(session, row)
            except Exception:  # noqa: BLE001 - chain is best-effort
                pass
    except Exception as exc:  # noqa: BLE001 - audit must never break the action
        logger.debug("SUPPORT | audit write failed (%s): %s", action, exc)


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def _minutes_ago_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Conversation settings (AI toggle + status)
# --------------------------------------------------------------------------- #
def get_settings(wa_number: str) -> Dict[str, Any]:
    """Return {ai_enabled, status} for a conversation (defaults: AI on / open)."""
    try:
        with session_scope() as session:
            row = session.get(ConversationSettings, wa_number)
            if row is None:
                return {"ai_enabled": True, "status": "open"}
            return {"ai_enabled": bool(row.ai_enabled), "status": row.status}
    except Exception as exc:  # noqa: BLE001 - never break callers (incl. webhook)
        logger.debug("SUPPORT | get_settings failed for %s: %s", wa_number, exc)
        return {"ai_enabled": True, "status": "open"}


def is_manual_mode(wa_number: str) -> bool:
    """True when the bot must NOT auto-reply (admin has taken over).

    Defaults to False (AI on) and is fully guarded, so a database hiccup can
    never accidentally silence the bot for every customer.
    """
    if not wa_number:
        return False
    try:
        return not get_settings(wa_number)["ai_enabled"]
    except Exception:  # noqa: BLE001
        return False


def set_ai_enabled(wa_number: str, enabled: bool, admin_user: str = "") -> Dict[str, Any]:
    """Enable/disable the bot for a conversation. Returns the new settings."""
    now = _now_iso()
    with session_scope() as session:
        row = session.get(ConversationSettings, wa_number)
        if row is None:
            row = ConversationSettings(
                wa_number=wa_number, ai_enabled=enabled, status="open",
                updated_by=admin_user or None, created_at=now, updated_at=now,
            )
            session.add(row)
        else:
            row.ai_enabled = enabled
            row.updated_by = admin_user or row.updated_by
            row.updated_at = now
    logger.info("SUPPORT | ai_enabled=%s for %s by %s", enabled, wa_number, admin_user or "?")
    return {"ai_enabled": enabled, "status": get_settings(wa_number)["status"]}


def set_status(wa_number: str, status: str, admin_user: str = "") -> str:
    """Set a conversation status (open|pending|closed)."""
    status = status if status in {"open", "pending", "closed"} else "open"
    now = _now_iso()
    with session_scope() as session:
        row = session.get(ConversationSettings, wa_number)
        if row is None:
            row = ConversationSettings(
                wa_number=wa_number, ai_enabled=True, status=status,
                updated_by=admin_user or None, created_at=now, updated_at=now,
            )
            session.add(row)
        else:
            row.status = status
            row.updated_by = admin_user or row.updated_by
            row.updated_at = now
    return status


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #
def assign(wa_number: str, assigned_to: Optional[str], assigned_by: str = "") -> Dict[str, Any]:
    """Assign (or unassign, with ``assigned_to=None``) a conversation."""
    now = _now_iso()
    with session_scope() as session:
        obj = (
            session.query(ConversationAssignment)
            .filter(ConversationAssignment.wa_number == wa_number)
            .first()
        )
        if obj is None:
            obj = ConversationAssignment(
                wa_number=wa_number, assigned_to=assigned_to, assigned_by=assigned_by or None,
                created_at=now, updated_at=now,
            )
            session.add(obj)
        else:
            obj.assigned_to = assigned_to
            obj.assigned_by = assigned_by or obj.assigned_by
            obj.updated_at = now
    logger.info("SUPPORT | assign %s -> %s by %s", wa_number, assigned_to, assigned_by or "?")
    return {"wa_number": wa_number, "assigned_to": assigned_to}


def get_assignment(wa_number: str) -> Optional[str]:
    """Return the admin a conversation is assigned to, or None."""
    try:
        with session_scope() as session:
            obj = (
                session.query(ConversationAssignment)
                .filter(ConversationAssignment.wa_number == wa_number)
                .first()
            )
            return obj.assigned_to if obj else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("SUPPORT | get_assignment failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Internal notes
# --------------------------------------------------------------------------- #
def add_note(wa_number: str, admin_user: str, note: str) -> Dict[str, Any]:
    """Add an admin-only note. Returns the created note dict."""
    note = (note or "").strip()
    if not note:
        raise ValueError("note is required")
    now = _now_iso()
    with session_scope() as session:
        obj = InternalNote(wa_number=wa_number, admin_user=admin_user, note=note[:5000], created_at=now)
        session.add(obj)
        session.flush()
        note_id = obj.id
    return {"id": note_id, "wa_number": wa_number, "admin_user": admin_user, "note": note, "created_at": now}


def list_notes(wa_number: str, limit: int = 100) -> List[Dict[str, Any]]:
    """List internal notes for a conversation, newest first."""
    with session_scope() as session:
        rows = (
            session.query(InternalNote)
            .filter(InternalNote.wa_number == wa_number)
            .order_by(InternalNote.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {"id": r.id, "admin_user": r.admin_user, "note": r.note, "created_at": r.created_at}
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Admin messages + status
# --------------------------------------------------------------------------- #
def record_admin_message(
    wa_number: str,
    admin_user: str,
    *,
    msg_type: str = "text",
    body: Optional[str] = None,
    media_id: Optional[str] = None,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
    wa_message_id: Optional[str] = None,
    status: str = "queued",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a console-sent message and return its dict representation."""
    now = _now_iso()
    with session_scope() as session:
        obj = AdminMessage(
            wa_number=wa_number, admin_user=admin_user, direction="out", msg_type=msg_type,
            body=body, media_id=media_id, filename=filename, mime_type=mime_type,
            wa_message_id=wa_message_id, status=status, error=error, created_at=now,
        )
        session.add(obj)
        session.flush()
        rec = _admin_msg_to_dict(obj)
    return rec


def mark_outbound(wa_number: str, snippet: str) -> None:
    """Update the inbox conversation summary after an admin sends a reply.

    Sets last_message/last_direction='out', bumps the timestamp + message count
    and clears the unread counter — mirroring what the bot path does, but without
    inserting into the ``messages`` table (admin sends live in ``admin_messages``).
    """
    now = _now_iso()
    text = (snippet or "")[:280]
    try:
        with session_scope() as session:
            conv = (
                session.query(DashConversation)
                .filter(DashConversation.wa_number == wa_number)
                .first()
            )
            if conv is None:
                session.add(DashConversation(
                    wa_number=wa_number, last_message=text, last_direction="out",
                    message_count=1, unread_count=0, status="open",
                    started_at=now, last_message_at=now,
                ))
            else:
                conv.last_message = text
                conv.last_direction = "out"
                conv.message_count = int(conv.message_count or 0) + 1
                conv.unread_count = 0
                conv.last_message_at = now
    except Exception as exc:  # noqa: BLE001
        logger.debug("SUPPORT | mark_outbound failed for %s: %s", wa_number, exc)


def mark_read(wa_number: str) -> None:
    """Clear the unread counter for a conversation (opened in the console)."""
    try:
        with session_scope() as session:
            conv = (
                session.query(DashConversation)
                .filter(DashConversation.wa_number == wa_number)
                .first()
            )
            if conv is not None:
                conv.unread_count = 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("SUPPORT | mark_read failed for %s: %s", wa_number, exc)


def record_status(wa_message_id: str, status: str, wa_number: str = "", timestamp: str = "") -> None:
    """Persist a WhatsApp status receipt and update the admin message's status."""
    if not wa_message_id or not status:
        return
    now = _now_iso()
    try:
        with session_scope() as session:
            session.add(
                MessageStatus(
                    wa_message_id=wa_message_id, wa_number=wa_number or None,
                    status=status, timestamp=timestamp or None, created_at=now,
                )
            )
            obj = (
                session.query(AdminMessage)
                .filter(AdminMessage.wa_message_id == wa_message_id)
                .first()
            )
            if obj is not None and _status_rank(status) >= _status_rank(obj.status):
                obj.status = status
    except Exception as exc:  # noqa: BLE001 - status tracking must never break webhook
        logger.debug("SUPPORT | record_status failed for %s: %s", wa_message_id, exc)


_STATUS_ORDER = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


def _status_rank(status: Optional[str]) -> int:
    return _STATUS_ORDER.get(status or "", 0)


# --------------------------------------------------------------------------- #
# Merged timeline
# --------------------------------------------------------------------------- #
def _admin_msg_to_dict(obj: AdminMessage) -> Dict[str, Any]:
    return {
        "source": "admin",
        "id": f"a{obj.id}",
        "direction": "out",
        "sender": "admin",
        "type": obj.msg_type,
        "text": obj.body or "",
        "media_id": obj.media_id,
        "filename": obj.filename,
        "mime_type": obj.mime_type,
        "status": obj.status,
        "admin_user": obj.admin_user,
        "created_at": obj.created_at,
    }


def thread(wa_number: str, limit: int = 500) -> List[Dict[str, Any]]:
    """Return the merged customer↔bot↔admin timeline, oldest first.

    Combines the existing ``messages`` table (customer 'in' + bot 'out') with
    ``admin_messages`` (console 'out'), sorted by ISO ``created_at``.
    """
    items: List[Dict[str, Any]] = []
    with session_scope() as session:
        msgs = (
            session.query(DashMessage)
            .filter(DashMessage.wa_number == wa_number)
            .order_by(DashMessage.id.desc())
            .limit(limit)
            .all()
        )
        for m in msgs:
            items.append({
                "source": "bot" if m.direction == "out" else "customer",
                "id": f"m{m.id}",
                "direction": m.direction,
                "sender": "bot" if m.direction == "out" else "customer",
                "type": "text",
                "text": m.text or "",
                "media_id": None,
                "filename": None,
                "mime_type": None,
                "status": None,
                "admin_user": None,
                "created_at": m.created_at,
            })
        admins = (
            session.query(AdminMessage)
            .filter(AdminMessage.wa_number == wa_number)
            .order_by(AdminMessage.id.desc())
            .limit(limit)
            .all()
        )
        items.extend(_admin_msg_to_dict(a) for a in admins)
    # ISO-8601 strings sort chronologically as plain strings.
    items.sort(key=lambda x: (x.get("created_at") or ""))
    return items[-limit:]


# --------------------------------------------------------------------------- #
# Enriched inbox
# --------------------------------------------------------------------------- #
def inbox(search: str = "", status_filter: str = "", only_unread: bool = False,
          limit: int = 200) -> List[Dict[str, Any]]:
    """Return the console inbox: conversations enriched with AI mode + assignment."""
    with session_scope() as session:
        q = session.query(DashConversation)
        if search:
            like = f"%{search}%"
            q = q.filter(
                (DashConversation.wa_number.like(like))
                | (DashConversation.profile_name.like(like))
            )
        if only_unread:
            q = q.filter(DashConversation.unread_count > 0)
        rows = q.order_by(DashConversation.last_message_at.desc()).limit(limit).all()

        # Pull settings + assignments in bulk to avoid N+1.
        settings = {s.wa_number: s for s in session.query(ConversationSettings).all()}
        assigns = {a.wa_number: a for a in session.query(ConversationAssignment).all()}

        out: List[Dict[str, Any]] = []
        for r in rows:
            s = settings.get(r.wa_number)
            a = assigns.get(r.wa_number)
            conv_status = s.status if s else "open"
            if status_filter and conv_status != status_filter:
                continue
            out.append({
                "wa_number": r.wa_number,
                "profile_name": r.profile_name or "",
                "last_message": r.last_message or "",
                "last_direction": r.last_direction or "",
                "last_message_at": r.last_message_at,
                "unread_count": int(r.unread_count or 0),
                "message_count": int(r.message_count or 0),
                "status": conv_status,
                "ai_enabled": bool(s.ai_enabled) if s else True,
                "assigned_to": a.assigned_to if a else None,
            })
        return out


# --------------------------------------------------------------------------- #
# Live statistics
# --------------------------------------------------------------------------- #
def live_stats() -> Dict[str, int]:
    """Return the console's live counters."""
    today = _today_start_iso()
    online_since = _minutes_ago_iso(5)
    with session_scope() as session:
        customers_online = (
            session.query(func.count(DashConversation.id))
            .filter(DashConversation.last_message_at >= online_since)
            .scalar()
        ) or 0
        pending_replies = (
            session.query(func.count(DashConversation.id))
            .filter(DashConversation.last_direction == "in")
            .scalar()
        ) or 0
        todays_messages = (
            session.query(func.count(DashMessage.id))
            .filter(DashMessage.created_at >= today)
            .scalar()
        ) or 0
        todays_orders = (
            session.query(func.count(DashOrder.id))
            .filter(DashOrder.looked_up_at >= today)
            .scalar()
        ) or 0
        manual_replies = (
            session.query(func.count(AdminMessage.id))
            .filter(AdminMessage.created_at >= today)
            .scalar()
        ) or 0
        # AI replies today = bot outbound messages today.
        ai_replies = (
            session.query(func.count(DashMessage.id))
            .filter(DashMessage.created_at >= today, DashMessage.direction == "out")
            .scalar()
        ) or 0
    return {
        "customers_online": int(customers_online),
        "pending_replies": int(pending_replies),
        "todays_messages": int(todays_messages),
        "todays_orders": int(todays_orders),
        "ai_replies": int(ai_replies),
        "manual_replies": int(manual_replies),
    }


# --------------------------------------------------------------------------- #
# Customer profile
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def customer_profile(wa_number: str) -> Dict[str, Any]:
    """Return the customer profile card shown beside the chat."""
    with session_scope() as session:
        cust = (
            session.query(DashCustomer)
            .filter(DashCustomer.wa_number == wa_number)
            .first()
        )
        conv = (
            session.query(DashConversation)
            .filter(DashConversation.wa_number == wa_number)
            .first()
        )
        orders = (
            session.query(DashOrder)
            .filter(DashOrder.wa_number == wa_number)
            .order_by(DashOrder.id.desc())
            .all()
        )

    # Deduplicate orders by order_name for count + spend.
    seen: Dict[str, float] = {}
    last_order = None
    for o in orders:
        name = o.order_name or f"#{o.id}"
        if name not in seen:
            seen[name] = _to_float(o.total_price)
        if last_order is None:
            last_order = {
                "order_name": o.order_name,
                "total_price": o.total_price,
                "currency": o.currency,
                "financial_status": o.financial_status,
                "fulfillment_status": o.fulfillment_status,
                "looked_up_at": o.looked_up_at,
            }

    return {
        "wa_number": wa_number,
        "profile_name": (cust.profile_name if cust else None) or (conv.profile_name if conv else None) or "",
        "language": (cust.language if cust else None) or "",
        "email": (cust.email if cust else None) or "",
        "first_seen_at": cust.first_seen_at if cust else None,
        "last_seen_at": cust.last_seen_at if cust else (conv.last_message_at if conv else None),
        "order_count": len(seen),
        "total_spend": round(sum(seen.values()), 2),
        "last_order": last_order,
        "conversation_count": 1 if conv else 0,
        "message_count": int(conv.message_count) if conv else 0,
        "ai_enabled": get_settings(wa_number)["ai_enabled"],
        "status": get_settings(wa_number)["status"],
        "assigned_to": get_assignment(wa_number),
    }


__all__ = [
    "get_settings", "is_manual_mode", "set_ai_enabled", "set_status",
    "assign", "get_assignment", "add_note", "list_notes",
    "record_admin_message", "record_status", "thread", "inbox",
    "live_stats", "customer_profile",
]
