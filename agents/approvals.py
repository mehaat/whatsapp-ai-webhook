"""
agents/approvals.py
--------------------
The v10.0 human-approval workflow for sensitive agent/admin actions.

High-risk tools in the shared registry (:mod:`agents.tools`) — issuing a refund,
running a WhatsApp broadcast, creating a coupon — are routed through the
pluggable *approval gate* installed here. Depending on configuration and simple
value thresholds, a high-risk tool call is either executed immediately or queued
as a pending :class:`~database.models.ApprovalRequest` for a manager to approve
or reject from the admin dashboard.

Design rules (mirroring :mod:`commerce.tenancy` / :mod:`admin.rbac`):
    * Nothing here raises. Every entry point is guarded and degrades to a
      sensible dict so the gate can never take down an agent turn or an admin
      page.
    * Every public function returns plain, detached ``dict``/``list`` values;
      callers never hold a live ORM session.
    * All database access goes through :func:`database.db.session_scope`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import config
from database.db import session_scope
from database.models import ApprovalRequest
from utils.logging import logger


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Return an ISO-8601 string for a datetime, or ``None``."""
    return dt.isoformat() if isinstance(dt, datetime) else None


def _dump(value: Any) -> str:
    """JSON-encode a value, degrading to ``"{}"`` on failure. Never raises."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - serialization must never break a decision
        return "{}"


def _loads(raw: Optional[str]) -> Dict[str, Any]:
    """Parse a JSON object string into a dict (never raises)."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _current_tenant_id() -> Optional[int]:
    """Best-effort current tenant id (``None`` when tenancy is unavailable)."""
    try:
        from commerce.tenancy import current_tenant_id

        return current_tenant_id()
    except Exception as exc:  # noqa: BLE001 - tenancy is optional here
        logger.debug("APPROVALS | tenant resolution skipped: %s", exc)
        return None


def _to_dict(req: ApprovalRequest) -> Dict[str, Any]:
    """Serialize an :class:`ApprovalRequest` to a plain, detached ``dict``."""
    parsed = _loads(req.payload)
    try:
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pretty = req.payload or "{}"
    return {
        "id": req.id,
        "tenant_id": req.tenant_id,
        "action": req.action,
        "payload": parsed,
        "payload_pretty": pretty,
        "reason": req.reason,
        "risk": req.risk,
        "requested_by": req.requested_by,
        "status": req.status,
        "result": req.result,
        "decided_by": req.decided_by,
        "decided_at": _iso(req.decided_at),
        "created_at": _iso(req.created_at),
    }


# --------------------------------------------------------------------------
# The approval gate
# --------------------------------------------------------------------------

def _broadcast_recipient_count(args: Dict[str, Any]) -> Optional[int]:
    """Resolve how many recipients a broadcast with these args would reach.

    Args:
        args: The ``send_broadcast`` tool arguments (``segment``/``tag``/
            ``consent_only``).

    Returns:
        The recipient count, or ``None`` when it cannot be determined.
    """
    try:
        from commerce.broadcast import recipient_count

        return recipient_count(
            segment=args.get("segment"),
            tag=args.get("tag"),
            consent_only=bool(args.get("consent_only", True)),
        )
    except Exception as exc:  # noqa: BLE001 - never let count resolution raise
        logger.error("APPROVALS | broadcast recipient_count failed: %s", exc)
        return None


def _queue_approval(tool: Any, args: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Create a pending :class:`ApprovalRequest` for ``tool`` and return the ack.

    Args:
        tool: The high-risk :class:`agents.tools.Tool` being gated.
        args: The tool call arguments.
        actor: Who requested the action (e.g. ``"agent"`` or a username).

    Returns:
        A ``pending_approval`` acknowledgement dict carrying the new request id.
    """
    try:
        with session_scope() as db:
            req = ApprovalRequest(
                tenant_id=_current_tenant_id(),
                action=tool.name,
                payload=_dump(args),
                reason=args.get("reason"),
                risk=getattr(tool, "risk", "high"),
                requested_by=actor or "agent",
                status="pending",
            )
            db.add(req)
            db.flush()  # populate req.id
            approval_id = req.id
        logger.info(
            "APPROVALS | queued %s (id=%s) requested_by=%s", tool.name, approval_id, actor
        )
        return {
            "ok": False,
            "status": "pending_approval",
            "approval_id": approval_id,
            "message": "This action has been queued for manager approval.",
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("APPROVALS | failed to queue %s: %s", tool.name, exc)
        return {"ok": False, "error": "approval queue error"}


def gate(tool: Any, args: Optional[Dict[str, Any]] = None, actor: str = "agent") -> Dict[str, Any]:
    """Decide how to handle a high-risk tool call. Never raises.

    Behaviour:
        * ``send_broadcast`` is auto-approved when its resolved recipient count
          is ``<= config.approval_broadcast_over`` — it executes immediately.
        * Otherwise, when ``config.approval_required`` is true, the call is
          queued as a pending :class:`ApprovalRequest` and a ``pending_approval``
          acknowledgement is returned.
        * When ``config.approval_required`` is false, the call executes
          immediately.

    Args:
        tool: The high-risk :class:`agents.tools.Tool` being gated.
        args: The tool call arguments (defaults to ``{}``).
        actor: Who requested the action (for auditing).

    Returns:
        Either the executed tool result (``{"ok": ..., "result"/"error": ...}``)
        or a ``pending_approval`` acknowledgement dict.
    """
    from agents.tools import execute_tool

    args = args or {}
    try:
        # Threshold auto-approve: small broadcasts run without sign-off.
        if getattr(tool, "name", None) == "send_broadcast":
            count = _broadcast_recipient_count(args)
            threshold = getattr(config, "approval_broadcast_over", 0)
            if count is not None and count <= threshold:
                logger.info(
                    "APPROVALS | auto-approving broadcast (%s <= %s recipients)",
                    count, threshold,
                )
                return execute_tool(tool, args)

        # Everything else: gate on the global approval switch.
        if getattr(config, "approval_required", True):
            return _queue_approval(tool, args, actor)

        return execute_tool(tool, args)
    except Exception as exc:  # noqa: BLE001 - the gate must never raise
        logger.error("APPROVALS | gate failed for %s: %s", getattr(tool, "name", "?"), exc)
        return {"ok": False, "error": "approval gate error"}


def install_gate() -> None:
    """Install :func:`gate` as the shared tool-registry approval gate.

    Call once at startup: thereafter every high-risk tool call routed through
    :func:`agents.tools.call_tool` consults this gate.
    """
    from agents import tools as t

    t.set_approval_gate(gate)
    logger.info("APPROVALS | approval gate installed")


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def approve(approval_id: int, *, decided_by: str = "admin") -> Dict[str, Any]:
    """Approve and execute a pending request. Never raises.

    Loads the pending :class:`ApprovalRequest`, resolves its tool via
    :func:`agents.tools.get_tool`, runs it with the stored payload, and records
    the outcome (status ``executed`` on success, ``failed`` otherwise) with the
    decider and an aware-UTC decision timestamp.

    Args:
        approval_id: The request's primary key.
        decided_by: Who approved the request (for auditing).

    Returns:
        ``{"ok": bool, "result"|"error": ..., "status": str}``.
    """
    from agents.tools import execute_tool, get_tool

    try:
        with session_scope() as db:
            req = db.get(ApprovalRequest, approval_id)
            if req is None:
                return {"ok": False, "error": "approval not found", "status": "unknown"}
            if req.status != "pending":
                return {"ok": False, "error": f"already {req.status}", "status": req.status}
            action = req.action
            payload = req.payload

        tool = get_tool(action)
        if tool is None:
            outcome = {"ok": False, "error": f"unknown tool: {action}"}
        else:
            outcome = execute_tool(tool, _loads(payload))

        status = "executed" if outcome.get("ok") else "failed"
        with session_scope() as db:
            req = db.get(ApprovalRequest, approval_id)
            if req is not None:
                req.status = status
                req.result = _dump(outcome)
                req.decided_by = decided_by
                req.decided_at = _utcnow()

        logger.info(
            "APPROVALS | %s approval #%s (%s) by %s", status, approval_id, action, decided_by
        )
        result: Dict[str, Any] = {"ok": bool(outcome.get("ok")), "status": status}
        if outcome.get("ok"):
            result["result"] = outcome.get("result")
        else:
            result["error"] = outcome.get("error")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("APPROVALS | approve #%s failed: %s", approval_id, exc)
        return {"ok": False, "error": str(exc), "status": "failed"}


def reject(
    approval_id: int, *, decided_by: str = "admin", reason: Optional[str] = None
) -> Dict[str, Any]:
    """Reject a pending request without executing it. Never raises.

    Args:
        approval_id: The request's primary key.
        decided_by: Who rejected the request (for auditing).
        reason: Optional rejection note (appended to the stored reason).

    Returns:
        ``{"ok": True, "status": "rejected"}`` (or an error dict on failure).
    """
    try:
        with session_scope() as db:
            req = db.get(ApprovalRequest, approval_id)
            if req is None:
                return {"ok": False, "error": "approval not found", "status": "unknown"}
            req.status = "rejected"
            req.decided_by = decided_by
            req.decided_at = _utcnow()
            if reason:
                req.reason = reason
        logger.info("APPROVALS | rejected approval #%s by %s", approval_id, decided_by)
        return {"ok": True, "status": "rejected"}
    except Exception as exc:  # noqa: BLE001
        logger.error("APPROVALS | reject #%s failed: %s", approval_id, exc)
        return {"ok": False, "error": str(exc), "status": "rejected"}


# --------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------

def list_approvals(status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Return approval requests as dicts (newest first). Never raises.

    Args:
        status: When set, filter to this status (``pending``/``approved``/
            ``rejected``/``executed``/``failed``).
        limit: Maximum number of rows to return.

    Returns:
        A list of serialized approval-request dicts (empty on error).
    """
    try:
        with session_scope() as db:
            q = db.query(ApprovalRequest)
            if status:
                q = q.filter(ApprovalRequest.status == status)
            rows = q.order_by(ApprovalRequest.id.desc()).limit(max(1, int(limit))).all()
            return [_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("APPROVALS | list_approvals failed: %s", exc)
        return []


def get_approval(approval_id: int) -> Optional[Dict[str, Any]]:
    """Return a single approval request by id, or ``None``. Never raises.

    Args:
        approval_id: The request's primary key.

    Returns:
        The serialized approval-request dict, or ``None`` if not found / error.
    """
    try:
        with session_scope() as db:
            req = db.get(ApprovalRequest, approval_id)
            return _to_dict(req) if req is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("APPROVALS | get_approval #%s failed: %s", approval_id, exc)
        return None


def pending_count() -> int:
    """Return the number of pending approval requests. Never raises."""
    try:
        with session_scope() as db:
            return (
                db.query(ApprovalRequest)
                .filter(ApprovalRequest.status == "pending")
                .count()
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("APPROVALS | pending_count failed: %s", exc)
        return 0
