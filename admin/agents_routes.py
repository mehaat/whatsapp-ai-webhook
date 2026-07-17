"""
admin/agents_routes.py
-----------------------
The admin "AI Agents" console (v10.0): a dashboard page that lists the
orchestrator's specialist agents and the tools each may use, an interactive chat
box for staff to talk to the orchestrator, and a table of recent ``AgentRun``
turns for auditing.

This is an additive blueprint mounted at ``/admin/agents``, protected by
:func:`admin.security.login_required`, and it shares the admin session, CSRF
token and base template. It never mutates the core ``/admin`` blueprint.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from flask import Blueprint, jsonify, render_template, request

from admin.security import (
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from agents.orchestrator import orchestrator
from config import config
from utils.logging import logger

admin_agents_bp = Blueprint(
    "admin_agents",
    __name__,
    url_prefix="/admin/agents",
    template_folder="templates",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_agents_bp.context_processor
def _inject_globals() -> Dict[str, Any]:
    """Expose the CSRF token + current user to this blueprint's templates."""
    return {
        "csrf_token": get_csrf_token() if is_authenticated() else "",
        "admin_user": current_user(),
        "app_version": _app_version(),
        "nav_active": request.endpoint,
    }


def _app_version() -> str:
    try:
        return config.version
    except Exception:  # noqa: BLE001
        return ""


def _recent_runs(limit: int = 25) -> List[Dict[str, Any]]:
    """Return the most recent ``AgentRun`` rows as plain dicts (never raises)."""
    try:
        from database.db import session_scope
        from database.models import AgentRun

        with session_scope() as db:
            rows = (
                db.query(AgentRun)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(limit)
                .all()
            )
            out: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    tools = json.loads(r.tools_used or "[]")
                except Exception:  # noqa: BLE001
                    tools = []
                out.append({
                    "id": r.id,
                    "agent": r.agent,
                    "channel": r.channel,
                    "wa_number": r.wa_number,
                    "intent": r.intent,
                    "user_message": r.user_message,
                    "reply": r.reply,
                    "tools_used": tools,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                })
            return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADMIN_AGENTS | recent runs query failed: %s", exc)
        return []


# --------------------------------------------------------------------------
# Console page
# --------------------------------------------------------------------------

@admin_agents_bp.route("/", methods=["GET"])
@login_required
def agents_home() -> Any:
    """Render the AI Agents console."""
    return render_template(
        "admin/agents_console.html",
        agents=orchestrator.list_agents(),
        runs=_recent_runs(),
        nav_active="admin_agents.agents_home",
    )


# --------------------------------------------------------------------------
# Chat endpoint (used by the page's fetch())
# --------------------------------------------------------------------------

@admin_agents_bp.route("/chat", methods=["POST"])
@login_required
@csrf_protect
def chat() -> Any:
    """Route a message from the console through the orchestrator; JSON reply."""
    if request.is_json:
        body = request.get_json(silent=True) or {}
    else:
        body = request.form
    message = str(body.get("message", "") or "").strip()
    if not message:
        return jsonify({"error": "message_required"}), 400

    wa_number = (body.get("wa_number") or "") or None
    response = orchestrator.route(
        message, {"channel": "console", "wa_number": wa_number},
    )
    return jsonify(response.to_dict())
