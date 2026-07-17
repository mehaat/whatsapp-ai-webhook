"""
agents/tools.py
----------------
The shared tool registry for the v10.0 multi-agent system.

A *tool* is a named, described, JSON-schema'd capability that wraps an existing
commerce service. Specialist agents, the MCP server, and the human-approval
workflow all operate over this one registry, so a capability is defined once and
reused everywhere.

High-risk tools (issue a refund, run a broadcast, create a coupon) are routed
through a pluggable approval gate: when human approval is required, calling such
a tool creates a pending ``ApprovalRequest`` instead of executing immediately.
Everything is guarded — a tool call never raises to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config import config
from utils.logging import logger


@dataclass
class Tool:
    """A callable capability exposed to agents and MCP clients."""

    name: str
    description: str
    handler: Callable[[Dict[str, Any]], Any]
    parameters: Dict[str, Any] = field(default_factory=dict)  # JSON Schema
    risk: str = "low"  # "low" | "high"
    category: str = "general"
    agents: Optional[List[str]] = None  # None => available to all agents


_TOOLS: Dict[str, Tool] = {}
# Pluggable approval gate. Set by agents.approvals; default executes inline.
_APPROVAL_GATE: Optional[Callable[[Tool, Dict[str, Any], str], Dict[str, Any]]] = None


def register_tool(tool: Tool) -> None:
    """Register (or replace) a tool by name."""
    _TOOLS[tool.name] = tool


def register(
    name: str, description: str, handler, *, parameters=None, risk="low",
    category="general", agents=None,
) -> None:
    """Convenience registration helper."""
    register_tool(Tool(name, description, handler, parameters or {}, risk, category, agents))


def get_tool(name: str) -> Optional[Tool]:
    return _TOOLS.get(name)


def list_tools(*, agent: Optional[str] = None, include_high: bool = True) -> List[Tool]:
    """List tools, optionally filtered to those available to ``agent``."""
    out = []
    for tool in _TOOLS.values():
        if not include_high and tool.risk == "high":
            continue
        if agent is not None and tool.agents is not None and agent not in tool.agents:
            continue
        out.append(tool)
    return out


def set_approval_gate(fn: Callable[[Tool, Dict[str, Any], str], Dict[str, Any]]) -> None:
    """Install the human-approval gate (called for high-risk tools)."""
    global _APPROVAL_GATE
    _APPROVAL_GATE = fn


def call_tool(
    name: str, args: Optional[Dict[str, Any]] = None, *, actor: str = "agent",
    allow_approval: bool = True,
) -> Dict[str, Any]:
    """Execute a tool by name, honouring the approval gate for high-risk tools.

    Returns a dict: ``{"ok": bool, "result": ..., "error": ...}`` or, when the
    action needs sign-off, ``{"ok": False, "status": "pending_approval",
    "approval_id": ...}``. Never raises.
    """
    args = args or {}
    tool = _TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {name}"}

    # High-risk actions go through the approval gate when configured.
    if (
        tool.risk == "high"
        and allow_approval
        and getattr(config, "approval_required", True)
        and _APPROVAL_GATE is not None
    ):
        try:
            return _APPROVAL_GATE(tool, args, actor)
        except Exception as exc:  # noqa: BLE001
            logger.error("TOOLS | approval gate failed for %s: %s", name, exc)
            return {"ok": False, "error": "approval gate error"}

    return execute_tool(tool, args)


def execute_tool(tool: Tool, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a tool's handler directly (used post-approval too). Never raises."""
    try:
        result = tool.handler(args or {})
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001 - a tool must never crash the agent
        logger.error("TOOLS | tool '%s' failed: %s", tool.name, exc)
        return {"ok": False, "error": str(exc)}


def mcp_tool_schemas(*, include_high: bool = True) -> List[Dict[str, Any]]:
    """Return MCP-style ``tools/list`` schemas for every registered tool."""
    schemas = []
    for tool in list_tools(include_high=include_high):
        schemas.append({
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters or {"type": "object", "properties": {}},
            "_meta": {"risk": tool.risk, "category": tool.category},
        })
    return schemas


# ==========================================================================
# Core tools — thin, guarded wrappers over existing commerce services.
# ==========================================================================

def _t_search_products(args: Dict[str, Any]) -> Any:
    from shopify.search import search_and_rank

    query = str(args.get("query", "")).strip()
    limit = int(args.get("limit", 5) or 5)
    matches = search_and_rank(query, limit=limit) or []
    return [m.to_card_dict() if hasattr(m, "to_card_dict") else m for m in matches]


def _t_order_status(args: Dict[str, Any]) -> Any:
    from commerce.service import order_service

    if args.get("order_number"):
        return order_service.get_order(order_number=str(args["order_number"]),
                                       include_tracking=True)
    if args.get("wa_number"):
        return order_service.latest_order_for(str(args["wa_number"]))
    return None


def _t_create_ticket(args: Dict[str, Any]) -> Any:
    from commerce.tickets import create_ticket

    return create_ticket(
        subject=str(args.get("subject", "Support request")),
        wa_number=args.get("wa_number"), body=args.get("body"),
        priority=str(args.get("priority", "normal")), author="agent",
    )


def _t_create_return(args: Dict[str, Any]) -> Any:
    from commerce.returns import create_return

    return create_return(
        int(args["order_id"]), kind=str(args.get("kind", "return")),
        reason=args.get("reason"), wa_number=args.get("wa_number"), actor="agent",
    )


def _t_recommend(args: Dict[str, Any]) -> Any:
    from commerce.recommendations import recommend_for_whatsapp

    return recommend_for_whatsapp(str(args.get("wa_number", "")),
                                  limit=int(args.get("limit", 5) or 5))


def _t_complete_look(args: Dict[str, Any]) -> Any:
    from commerce.stylist import complete_the_look

    return complete_the_look(
        product_type=args.get("product_type"), color=args.get("color"),
        occasion=args.get("occasion"),
    )


def _t_check_stock(args: Dict[str, Any]) -> Any:
    from commerce.stock import resolve_variant_id
    from shopify.inventory import check_variant_inventory

    variant = resolve_variant_id({"product_retailer_id": args.get("retailer_id"),
                                  "variant_id": args.get("variant_id")})
    if variant is None:
        return {"available": None, "reason": "unresolved variant"}
    status = check_variant_inventory(variant)
    if status is None:
        return {"available": None, "reason": "no inventory record"}
    return {"available": status.available, "quantity": status.quantity,
            "product": status.product_title}


def _t_analytics_summary(args: Dict[str, Any]) -> Any:
    from commerce.analytics import order_summary

    return order_summary()


def _t_sales_report(args: Dict[str, Any]) -> Any:
    from commerce.reports import sales_report

    return sales_report(group=str(args.get("group", "day")))


def _t_issue_coupon(args: Dict[str, Any]) -> Any:
    from commerce.discounts import create_coupon

    return create_coupon(
        code=args.get("code"), kind=str(args.get("kind", "percent")),
        value=float(args.get("value", 0) or 0), min_order=float(args.get("min_order", 0) or 0),
    )


def _t_send_broadcast(args: Dict[str, Any]) -> Any:
    from commerce.broadcast import send_broadcast

    return send_broadcast(
        str(args.get("message", "")), segment=args.get("segment"),
        tag=args.get("tag"), consent_only=bool(args.get("consent_only", True)), actor="agent",
    )


def _t_issue_refund(args: Dict[str, Any]) -> Any:
    from commerce.service import order_service

    order_id = int(args["order_id"])
    order_service.set_payment_status(order_id, "refunded", actor="agent")
    return order_service.set_status(order_id, "refunded", actor="agent",
                                    note=str(args.get("reason", "agent refund")))


def register_core_tools() -> None:
    """Register the built-in tool set. Idempotent."""
    register("search_products", "Search the store catalogue for products matching a query.",
             _t_search_products, parameters={"type": "object", "properties": {
                 "query": {"type": "string"}, "limit": {"type": "integer"}},
                 "required": ["query"]}, category="sales", agents=None)
    register("order_status", "Look up an order's status by order number or customer WhatsApp number.",
             _t_order_status, parameters={"type": "object", "properties": {
                 "order_number": {"type": "string"}, "wa_number": {"type": "string"}}},
             category="support")
    register("create_support_ticket", "Open a customer support ticket.",
             _t_create_ticket, parameters={"type": "object", "properties": {
                 "subject": {"type": "string"}, "wa_number": {"type": "string"},
                 "body": {"type": "string"}}, "required": ["subject"]}, category="support")
    register("create_return", "Create a return/refund/exchange request for an order.",
             _t_create_return, parameters={"type": "object", "properties": {
                 "order_id": {"type": "integer"}, "kind": {"type": "string"},
                 "reason": {"type": "string"}, "wa_number": {"type": "string"}},
                 "required": ["order_id"]}, category="support")
    register("recommend_products", "Get personalized product recommendations for a customer.",
             _t_recommend, parameters={"type": "object", "properties": {
                 "wa_number": {"type": "string"}, "limit": {"type": "integer"}}},
             category="sales")
    register("complete_the_look", "Get stylist outfit suggestions for a product type/occasion.",
             _t_complete_look, parameters={"type": "object", "properties": {
                 "product_type": {"type": "string"}, "color": {"type": "string"},
                 "occasion": {"type": "string"}}}, category="sales")
    register("check_stock", "Check live inventory for a catalogue item.",
             _t_check_stock, parameters={"type": "object", "properties": {
                 "retailer_id": {"type": "string"}, "variant_id": {"type": "string"}}},
             category="inventory")
    register("analytics_summary", "Get today's/this month's order + revenue summary.",
             _t_analytics_summary, parameters={"type": "object", "properties": {}},
             category="analytics")
    register("sales_report", "Generate a sales report grouped by day or month.",
             _t_sales_report, parameters={"type": "object", "properties": {
                 "group": {"type": "string", "enum": ["day", "month"]}}}, category="analytics")
    # --- high-risk (approval-gated) ---
    register("issue_coupon", "Create a discount coupon.", _t_issue_coupon,
             parameters={"type": "object", "properties": {
                 "code": {"type": "string"}, "kind": {"type": "string"},
                 "value": {"type": "number"}, "min_order": {"type": "number"}}},
             risk="high", category="marketing")
    register("send_broadcast", "Send a WhatsApp broadcast to a customer segment.",
             _t_send_broadcast, parameters={"type": "object", "properties": {
                 "message": {"type": "string"}, "segment": {"type": "string"},
                 "tag": {"type": "string"}}, "required": ["message"]},
             risk="high", category="marketing")
    register("issue_refund", "Refund an order (marks it refunded).", _t_issue_refund,
             parameters={"type": "object", "properties": {
                 "order_id": {"type": "integer"}, "reason": {"type": "string"}},
                 "required": ["order_id"]}, risk="high", category="support")


register_core_tools()
