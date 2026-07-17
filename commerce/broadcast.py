"""
commerce/broadcast.py
----------------------
The v7.0 WhatsApp broadcast manager.

Resolves a set of recipient WhatsApp numbers from the CRM
(:class:`~database.models.CrmProfile`) — filtered by ``segment`` and/or ``tag``,
and (by default) restricted to customers who granted marketing consent — then
fans the send out onto the durable job queue: one ``broadcast_message`` job per
recipient (via :func:`commerce.jobs.run_async`). The job handler lazily imports
:func:`whatsapp.sender.send_text_message`, so importing this module never
touches the network.

Every public function is defensive and never raises.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from database.db import session_scope
from database.models import CrmProfile
from utils.logging import logger

# Job kind used for a single outbound broadcast message.
BROADCAST_JOB_KIND = "broadcast_message"


# --------------------------------------------------------------------------
# Recipient resolution
# --------------------------------------------------------------------------

def _recipient_query(session, segment: Optional[str], tag: Optional[str], consent_only: bool):
    """Build the filtered CrmProfile query for a broadcast audience."""
    q = session.query(CrmProfile)
    if segment:
        q = q.filter(CrmProfile.segment == segment)
    if tag:
        q = q.filter(CrmProfile.tags.ilike(f"%{tag}%"))
    if consent_only:
        q = q.filter(CrmProfile.marketing_consent.is_(True))
    return q


def _resolve_recipients(
    segment: Optional[str], tag: Optional[str], consent_only: bool
) -> List[str]:
    """Return the de-duplicated list of recipient wa_numbers. Never raises."""
    try:
        with session_scope() as session:
            rows = _recipient_query(session, segment, tag, consent_only).all()
            seen: List[str] = []
            for r in rows:
                if r.wa_number and r.wa_number not in seen:
                    seen.append(r.wa_number)
            return seen
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | broadcast recipient resolution failed: %s", exc)
        return []


def recipient_count(
    segment: Optional[str] = None,
    tag: Optional[str] = None,
    consent_only: bool = True,
) -> int:
    """Count the recipients a broadcast with these filters would target."""
    try:
        with session_scope() as session:
            return _recipient_query(session, segment, tag, consent_only).count()
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | recipient_count failed: %s", exc)
        return 0


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def send_broadcast(
    message: str,
    *,
    segment: Optional[str] = None,
    tag: Optional[str] = None,
    consent_only: bool = True,
    actor: str = "admin",
) -> Dict[str, Any]:
    """Enqueue a broadcast to the resolved audience. Never raises.

    Resolves recipients from the CRM, then enqueues one ``broadcast_message``
    job per recipient via :func:`commerce.jobs.run_async` (which runs inline
    when jobs are disabled). The per-message handler sends the WhatsApp text.

    Args:
        message: The message body to broadcast.
        segment: Restrict to customers in this CRM segment.
        tag: Restrict to customers whose CRM tags contain this substring.
        consent_only: When True (default), only marketing-consented customers.
        actor: Who is sending (for auditing).

    Returns:
        ``{"ok": bool, "recipients": int}``.
    """
    message = (message or "").strip()
    if not message:
        return {"ok": False, "recipients": 0, "error": "empty_message"}

    recipients = _resolve_recipients(segment, tag, consent_only)
    if not recipients:
        logger.info("COMMERCE | broadcast by %s matched no recipients", actor)
        return {"ok": True, "recipients": 0}

    try:
        from commerce.jobs import run_async

        for wa_number in recipients:
            run_async(BROADCAST_JOB_KIND, {"wa_number": wa_number, "message": message})
    except Exception as exc:  # noqa: BLE001 - enqueue is best-effort
        logger.error("COMMERCE | broadcast enqueue failed: %s", exc)
        return {"ok": False, "recipients": len(recipients), "error": str(exc)}

    # Best-effort audit (never breaks the send).
    try:
        from commerce.service import order_service

        order_service.audit(
            actor=actor or "admin", action="broadcast.send", entity="broadcast",
            detail=f"recipients={len(recipients)} segment={segment or '-'} tag={tag or '-'}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("COMMERCE | broadcast audit skipped: %s", exc)

    logger.info("COMMERCE | broadcast enqueued for %s recipient(s) by %s",
                len(recipients), actor)
    return {"ok": True, "recipients": len(recipients)}


# --------------------------------------------------------------------------
# Job handler
# --------------------------------------------------------------------------

def _handle_broadcast_message(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Send a single broadcast WhatsApp message (job handler)."""
    wa_number = payload.get("wa_number")
    message = payload.get("message")
    if not wa_number or not message:
        logger.warning("COMMERCE | broadcast_message: missing wa_number/message")
        return None
    from whatsapp.sender import send_text_message

    ok = send_text_message(wa_number, message)
    return {"wa_number": wa_number, "sent": bool(ok)}


def register_broadcast_handler() -> None:
    """Register the ``broadcast_message`` job handler with the job queue."""
    try:
        from commerce.jobs import register_handler

        register_handler(BROADCAST_JOB_KIND, _handle_broadcast_message)
        logger.info("COMMERCE | broadcast_message handler registered")
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | register_broadcast_handler failed: %s", exc)
