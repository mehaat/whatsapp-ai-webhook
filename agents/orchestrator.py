"""
agents/orchestrator.py
-----------------------
The v10.0 multi-agent orchestrator: the single entry point that routes an
inbound customer/staff message to the right specialist agent and records the
turn for the admin console.

Routing is deterministic and offline-safe. :meth:`Orchestrator.classify` maps a
message to a specialist name by combining the existing rule-based commerce
intent detector (:func:`commerce.intent.detect_intent`) with a few keyword hints
(reports/revenue -> analytics, stock -> inventory, broadcast/coupon ->
marketing). :meth:`Orchestrator.route` classifies, dispatches to that
specialist's :meth:`Agent.handle`, and best-effort logs an ``AgentRun`` row.

Nothing here raises to the caller: any failure degrades to a friendly, generic
:class:`AgentResponse` so the WhatsApp handler and API never 500.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agents.base import AgentResponse
from agents.specialists import SPECIALIST_NAMES, get_specialists
from utils.logging import logger

# Intents (from commerce.intent.detect_intent) that map to the support agent.
_SUPPORT_INTENTS = {
    "track_order", "return", "refund", "cancel", "delivery_time",
    "invoice", "payment", "support", "human_agent", "escalation",
}
# Intents that map to the sales agent.
_SALES_INTENTS = {"browse_products", "place_order", "greeting"}


class Orchestrator:
    """Classifies messages and dispatches them to specialist agents."""

    def __init__(self) -> None:
        self._specialists = get_specialists()

    # ----------------------------------------------------------------------
    # Classification
    # ----------------------------------------------------------------------

    def classify(self, message: str) -> str:
        """Map a message to a specialist name.

        Combines keyword hints (checked first, most specific) with the shared
        commerce intent detector. Defaults to ``"sales"``.

        Args:
            message: The inbound free-text message.

        Returns:
            One of :data:`agents.specialists.SPECIALIST_NAMES`.
        """
        low = (message or "").lower().strip()
        if not low:
            return "sales"

        # 1) Direct keyword hints for the non-commerce specialists that the
        #    intent detector does not distinguish (analytics / inventory /
        #    marketing).
        if any(k in low for k in ("sales report", "report", "revenue", "analytics",
                                  "how many orders", "summary", "dashboard")):
            return "analytics"
        if any(k in low for k in ("stock", "in stock", "inventory", "available",
                                  "availability")):
            return "inventory"
        if any(k in low for k in ("broadcast", "campaign", "coupon", "promo code")):
            return "marketing"

        # 2) Fall back to the shared rule-based intent detector.
        try:
            from commerce.intent import detect_intent

            intent = detect_intent(message)
        except Exception as exc:  # noqa: BLE001 - classification must not raise
            logger.debug("ORCH | intent detection failed: %s", exc)
            intent = "unknown"

        if intent in _SUPPORT_INTENTS:
            return "support"
        if intent in _SALES_INTENTS:
            return "sales"
        if intent == "coupon":
            return "marketing"
        if intent == "stock":
            return "inventory"

        # 3) Default.
        return "sales"

    # ----------------------------------------------------------------------
    # Routing
    # ----------------------------------------------------------------------

    def route(self, message: str, context: Optional[Dict[str, Any]] = None) -> AgentResponse:
        """Classify and dispatch a message to the chosen specialist.

        Logs an ``AgentRun`` row best-effort. Never raises: on any error a
        friendly :class:`AgentResponse` is returned.

        Args:
            message: The inbound message text.
            context: Optional context dict (``channel``, ``wa_number`` ...).

        Returns:
            The specialist's :class:`AgentResponse`.
        """
        context = dict(context or {})
        name = self.classify(message)
        agent = self._specialists.get(name)

        if agent is None:  # pragma: no cover - classify only returns known names
            return AgentResponse(
                "sales",
                "I'm here to help — could you tell me a little more about what you need?",
            )

        try:
            response = agent.handle(message, context)
        except Exception as exc:  # noqa: BLE001 - an agent must never crash routing
            logger.error("ORCH | agent '%s' failed: %s", name, exc)
            response = AgentResponse(
                name,
                "Sorry, something went wrong on our side. Please try again in a "
                "moment, or type 'agent' to reach a human.",
            )

        self._log_run(name, message, response, context)
        return response

    def _log_run(
        self, name: str, message: str, response: AgentResponse, context: Dict[str, Any],
    ) -> None:
        """Best-effort persistence of an ``AgentRun`` audit row (never raises)."""
        try:
            from database.db import session_scope
            from database.models import AgentRun

            tenant_id = None
            try:
                from commerce.tenancy import current_tenant_id

                tenant_id = current_tenant_id()
            except Exception:  # noqa: BLE001
                tenant_id = None

            with session_scope() as db:
                db.add(AgentRun(
                    tenant_id=tenant_id,
                    agent=name,
                    channel=str(context.get("channel", "api"))[:16],
                    wa_number=(context.get("wa_number") or None),
                    intent=name,
                    user_message=(message or "")[:4000],
                    reply=(response.text or "")[:4000],
                    tools_used=json.dumps(response.tools_used or []),
                ))
        except Exception as exc:  # noqa: BLE001 - logging must not break routing
            logger.debug("ORCH | AgentRun logging failed: %s", exc)

    # ----------------------------------------------------------------------
    # Introspection
    # ----------------------------------------------------------------------

    def list_agents(self) -> List[Dict[str, Any]]:
        """Return a JSON-friendly description of every specialist agent."""
        out: List[Dict[str, Any]] = []
        specialists = self._specialists
        for name in SPECIALIST_NAMES:
            agent = specialists.get(name)
            if agent is None:
                continue
            out.append({
                "name": agent.name,
                "description": agent.description,
                "tools": list(agent.tools),
            })
        return out


# Module singleton — import and reuse everywhere.
orchestrator = Orchestrator()
