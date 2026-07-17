"""
agents/agent_api.py
--------------------
The JSON API surface for the v10.0 multi-agent orchestrator.

Two endpoints, mounted with no URL prefix so they live directly under ``/api``:

    * ``POST /api/agent``        — authenticated; route a message through the
      orchestrator and return the resulting :class:`AgentResponse` as JSON.
    * ``GET  /api/agent/agents`` — public; list the available specialist agents
      and their tools (useful for clients / documentation).

Every handler is guarded and returns JSON; a bad request yields ``400`` and the
orchestrator itself never raises (it degrades to a friendly reply).
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from agents.orchestrator import orchestrator
from commerce.auth import require_api_auth
from utils.logging import logger

agent_api_bp = Blueprint("agent_api", __name__)


@agent_api_bp.route("/api/agent", methods=["POST"])
@require_api_auth
def agent_route() -> Any:
    """Route a message through the orchestrator and return its reply."""
    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        message = str(body.get("message", "") or "").strip()
        if not message:
            return jsonify({"error": "message_required"}), 400

        context = {
            "channel": str(body.get("channel", "api") or "api"),
            "wa_number": body.get("wa_number") or None,
        }
        response = orchestrator.route(message, context)
        return jsonify(response.to_dict())
    except Exception as exc:  # noqa: BLE001 - the API must never 500 on input
        logger.error("AGENT_API | /api/agent failed: %s", exc)
        return jsonify({"error": "internal_error"}), 500


@agent_api_bp.route("/api/agent/agents", methods=["GET"])
def agent_list() -> Any:
    """Public: list the available specialist agents and their tools."""
    try:
        return jsonify({"agents": orchestrator.list_agents()})
    except Exception as exc:  # noqa: BLE001
        logger.error("AGENT_API | /api/agent/agents failed: %s", exc)
        return jsonify({"agents": []})
