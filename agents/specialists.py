"""
agents/specialists.py
----------------------
The five configured specialist agents that make up the ME-HAAT Fashion AI Bot
v10.0 multi-agent system. Each is a plain :class:`agents.base.Agent` built by a
small factory (:func:`_make_agent`) with a strong, brand-appropriate system
prompt (an Indian ethnic-wear retail voice), the subset of registry tools it may
use, and a keyword router mapping inbound phrases to those tools.

The orchestrator (``agents.orchestrator``) classifies each message to one of
these specialists and dispatches to its :meth:`Agent.handle`. Because the agents
are pure configuration over the shared, guarded tool registry, they work fully
offline (deterministic fallback replies) when no Gemini key is present.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from agents.base import Agent

# The canonical ordered list of specialist names.
SPECIALIST_NAMES: List[str] = ["sales", "support", "inventory", "marketing", "analytics"]

# A shared voice preamble prepended to every specialist prompt so the whole
# system speaks with one warm, professional ethnic-wear retail personality.
_BRAND_VOICE = (
    "You are a specialist assistant for ME-HAAT, a premium Indian ethnic-wear "
    "fashion house selling sarees, lehengas, kurtis, salwar suits, sherwanis and "
    "festive & bridal wear on WhatsApp. Speak warmly and professionally, like a "
    "knowledgeable in-store stylist. Be concise, respectful and helpful. Use only "
    "the verified data provided by your tools — never invent prices, stock, order "
    "numbers or delivery dates. When you lack data, ask one clear follow-up "
    "question. You may mix light Hinglish if the customer does."
)


def _make_agent(
    name: str,
    description: str,
    prompt: str,
    *,
    tools: List[str],
    keyword_routes: Optional[Dict[str, str]] = None,
    default_tool: Optional[str] = None,
) -> Agent:
    """Factory: build a configured :class:`Agent` with the shared brand voice."""
    system_prompt = f"{_BRAND_VOICE}\n\n{prompt.strip()}"
    return Agent(
        name,
        description,
        system_prompt,
        tools=tools,
        keyword_routes=keyword_routes or {},
        default_tool=default_tool,
    )


def _build_specialists() -> Dict[str, Agent]:
    """Construct a fresh dict of the five specialist agents."""
    sales = _make_agent(
        "sales",
        "Product discovery, recommendations and outfit styling.",
        (
            "You are the Sales & Styling specialist. Help customers discover and "
            "choose ethnic wear: search the catalogue, recommend pieces suited to "
            "their taste and occasion, and complete-the-look with matching blouses, "
            "dupattas, jewellery and footwear. Highlight fabric, colour, occasion "
            "and fit. Gently guide the customer toward a confident purchase without "
            "being pushy."
        ),
        tools=["search_products", "recommend_products", "complete_the_look"],
        keyword_routes={
            "recommend": "recommend_products",
            "suggest": "recommend_products",
            "wear": "complete_the_look",
            "style": "complete_the_look",
            "match": "complete_the_look",
        },
        default_tool="search_products",
    )

    support = _make_agent(
        "support",
        "Order tracking, returns, exchanges, refunds and support tickets.",
        (
            "You are the Customer Support specialist. Help customers track orders, "
            "start returns, exchanges and refunds, and raise support tickets when a "
            "human needs to step in. Be reassuring and solution-focused, especially "
            "when a customer is unhappy. Confirm order numbers before acting and set "
            "clear expectations about next steps and timelines."
        ),
        tools=["order_status", "create_return", "create_support_ticket", "issue_refund"],
        keyword_routes={
            "refund": "create_return",
            "return": "create_return",
            "exchange": "create_return",
            "track": "order_status",
            "where is my order": "order_status",
            "status": "order_status",
            "agent": "create_support_ticket",
            "complaint": "create_support_ticket",
            "help": "create_support_ticket",
        },
        default_tool="order_status",
    )

    inventory = _make_agent(
        "inventory",
        "Live stock and availability checks.",
        (
            "You are the Inventory specialist. Answer questions about live stock and "
            "availability for catalogue items and variants (size, colour). State "
            "clearly whether an item is in stock and, when known, the quantity. If a "
            "variant cannot be resolved, ask the customer which product, size or "
            "colour they mean."
        ),
        tools=["check_stock"],
        keyword_routes={
            "stock": "check_stock",
            "available": "check_stock",
            "in stock": "check_stock",
        },
        default_tool="check_stock",
    )

    marketing = _make_agent(
        "marketing",
        "Campaigns, broadcasts and discount coupons (approval-gated).",
        (
            "You are the Marketing specialist. Help staff plan and launch WhatsApp "
            "broadcasts to customer segments and create discount coupons and festive "
            "offers. These are high-impact actions that require manager approval "
            "before they take effect — always make that clear, and craft on-brand, "
            "consent-respecting campaign copy suited to Indian festive occasions."
        ),
        tools=["send_broadcast", "issue_coupon"],
        keyword_routes={
            "broadcast": "send_broadcast",
            "coupon": "issue_coupon",
            "discount": "issue_coupon",
            "offer": "issue_coupon",
        },
        default_tool=None,
    )

    analytics = _make_agent(
        "analytics",
        "Sales reports and business analytics summaries.",
        (
            "You are the Analytics specialist. Help staff understand business "
            "performance: order and revenue summaries and sales reports grouped by "
            "day or month. Present the verified numbers plainly, in Indian Rupees "
            "where relevant, and surface the one or two figures that matter most "
            "rather than dumping every metric."
        ),
        tools=["analytics_summary", "sales_report"],
        keyword_routes={
            "sales report": "sales_report",
            "report": "sales_report",
            "revenue": "analytics_summary",
            "how many orders": "analytics_summary",
            "summary": "analytics_summary",
        },
        default_tool="analytics_summary",
    )

    return {
        "sales": sales,
        "support": support,
        "inventory": inventory,
        "marketing": marketing,
        "analytics": analytics,
    }


# Module-level singleton set of specialists.
_SPECIALISTS: Dict[str, Agent] = _build_specialists()


def get_specialists() -> Dict[str, Agent]:
    """Return the mapping of specialist name -> configured :class:`Agent`."""
    return _SPECIALISTS
