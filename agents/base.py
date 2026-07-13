"""
agents/base.py
---------------
The generic specialist-agent used by the v10.0 orchestrator.

An :class:`Agent` has a role (name, description, system prompt), a set of tools
it may use, and a keyword router that maps an inbound message to one of those
tools. ``handle()`` selects a tool, extracts arguments from the message +
context, invokes it through the approval-aware registry, and composes a reply —
using Gemini to phrase the final answer when a key is configured, and a clean
deterministic template otherwise (so agents work fully offline/in tests).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from utils.logging import logger

from agents import tools as tool_registry


@dataclass
class AgentResponse:
    """The result of an agent handling a message."""

    agent: str
    text: str
    tools_used: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    needs_approval: bool = False
    approval_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent, "text": self.text, "tools_used": self.tools_used,
            "data": self.data, "needs_approval": self.needs_approval,
            "approval_id": self.approval_id,
        }


def gemini_available() -> bool:
    try:
        from config import config

        return bool(config.gemini_api_key)
    except Exception:  # noqa: BLE001
        return False


def synthesize(system_prompt: str, user_message: str, context_text: str) -> Optional[str]:
    """Phrase a reply with Gemini given tool output; None if unavailable."""
    if not gemini_available():
        return None
    try:
        from ai.gemini import generate_reply

        reply = generate_reply(
            history=[], customer_name="", language="english",
            verified_context=context_text, user_message=user_message,
        )
        if reply and reply not in {"QUOTA_EXCEEDED"}:
            return reply
    except Exception as exc:  # noqa: BLE001
        logger.debug("AGENT | gemini synthesis failed: %s", exc)
    return None


class Agent:
    """A configurable specialist agent."""

    def __init__(
        self, name: str, description: str, system_prompt: str, *,
        tools: Optional[List[str]] = None,
        keyword_routes: Optional[Dict[str, str]] = None,
        arg_builder: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        default_tool: Optional[str] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.keyword_routes = keyword_routes or {}
        self.arg_builder = arg_builder
        self.default_tool = default_tool

    def select_tool(self, message: str) -> Optional[str]:
        """Pick a tool for the message via keyword routing."""
        low = (message or "").lower()
        for keyword, tool_name in self.keyword_routes.items():
            if keyword in low:
                return tool_name
        return self.default_tool

    def build_args(self, message: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Assemble tool arguments from the message + conversation context."""
        args: Dict[str, Any] = {}
        if context.get("wa_number"):
            args["wa_number"] = context["wa_number"]
        # Order number like MH-2026-000123.
        m = re.search(r"\b([A-Z]{2}-\d{4}-\d{4,8})\b", message or "")
        if m:
            args["order_number"] = m.group(1)
        args["query"] = message
        if self.arg_builder:
            try:
                args.update(self.arg_builder(message, context) or {})
            except Exception as exc:  # noqa: BLE001
                logger.debug("AGENT | arg_builder failed: %s", exc)
        return args

    def handle(self, message: str, context: Optional[Dict[str, Any]] = None) -> AgentResponse:
        """Handle a message end to end; never raises."""
        context = context or {}
        tools_used: List[str] = []
        tool_name = self.select_tool(message)

        result_payload: Any = None
        needs_approval = False
        approval_id = None

        if tool_name and tool_registry.get_tool(tool_name) is not None:
            args = self.build_args(message, context)
            outcome = tool_registry.call_tool(tool_name, args, actor=f"agent:{self.name}")
            tools_used.append(tool_name)
            if outcome.get("status") == "pending_approval":
                needs_approval = True
                approval_id = outcome.get("approval_id")
                text = outcome.get("message") or (
                    "This action needs manager approval — I've submitted it for review."
                )
                return AgentResponse(self.name, text, tools_used,
                                     {"pending": True}, True, approval_id)
            result_payload = outcome.get("result") if outcome.get("ok") else None

        # Compose the reply text.
        context_text = _summarize_for_prompt(tool_name, result_payload)
        text = synthesize(self.system_prompt, message, context_text) \
            or _fallback_text(self.name, tool_name, result_payload)

        return AgentResponse(self.name, text, tools_used,
                             {"tool": tool_name, "result": result_payload},
                             needs_approval, approval_id)


def _summarize_for_prompt(tool_name: Optional[str], payload: Any) -> str:
    if payload is None:
        return "No tool data was available."
    try:
        text = json.dumps(payload, default=str)[:3000]
    except Exception:  # noqa: BLE001
        text = str(payload)[:3000]
    return f"Verified data from tool '{tool_name}':\n{text}"


def _fallback_text(agent: str, tool_name: Optional[str], payload: Any) -> str:
    """A clean, deterministic reply when no LLM is available."""
    if payload is None:
        return ("I couldn't find anything for that just now. Could you share a bit more "
                "detail (e.g. an order number, or what you're looking for)?")
    if isinstance(payload, list):
        if not payload:
            return "I didn't find any matches for that."
        names = []
        for item in payload[:5]:
            if isinstance(item, dict):
                names.append(str(item.get("title") or item.get("product_name")
                                 or item.get("order_number") or item.get("name") or item))
            else:
                names.append(str(item))
        return "Here's what I found: " + "; ".join(names)
    if isinstance(payload, dict):
        if payload.get("order_number"):
            return (f"Order {payload['order_number']} — status: {payload.get('status','?')}, "
                    f"payment: {payload.get('payment_status','?')}.")
        if payload.get("ticket_number"):
            return f"I've opened ticket {payload['ticket_number']} for you."
        if payload.get("rma_number"):
            return f"Your request {payload['rma_number']} has been logged."
        # Generic dict summary.
        keys = ", ".join(f"{k}: {v}" for k, v in list(payload.items())[:6])
        return f"Here's the summary — {keys}"
    return str(payload)[:1000]
