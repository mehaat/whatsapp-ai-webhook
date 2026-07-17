"""
commerce/personal_shopper.py
-----------------------------
The **Personal Shopping Assistant** for ME-HAAT Fashion AI Bot v9.0.

A lightweight, stateful guided-shopping flow for WhatsApp. It builds a small
per-customer profile (occasion, budget, colour, fabric, category) from free-form
messages, asks the *next* missing clarifying question, and — once it knows
enough — runs a verified Shopify search and returns a friendly, styled summary.

State is kept in a small in-process dict keyed by WhatsApp number (mirroring the
approach in :mod:`memory.store`). Design contract: **no public function ever
raises**; Shopify being disconnected simply yields a helpful fallback message.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from utils.logging import logger

# Per-customer shopping profiles: wa_number -> profile dict.
_PROFILES: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()

# The preference keys we try to collect, in the order we ask about them.
_PROFILE_KEYS = ("occasion", "max_budget", "color", "fabric", "category")

_MAX_RESULTS = 5


def _get_profile(wa_number: str) -> Dict[str, Any]:
    """Return (creating if needed) the mutable profile for a customer."""
    with _LOCK:
        profile = _PROFILES.get(wa_number)
        if profile is None:
            profile = {
                "occasion": None,
                "max_budget": None,
                "min_budget": None,
                "color": None,
                "fabric": None,
                "category": None,
                "asked": [],       # questions already asked (avoid repeats)
                "searched": False,
            }
            _PROFILES[wa_number] = profile
        return profile


def _merge_filters(profile: Dict[str, Any], text: str) -> None:
    """Update ``profile`` in place with any filters extracted from ``text``."""
    try:
        from shopify.search import extract_search_filters

        filters = extract_search_filters(text or "")
    except Exception as exc:  # noqa: BLE001 - extractor must never break the flow
        logger.debug("SHOPPER | filter extraction failed: %s", exc)
        return
    for key in ("max_budget", "min_budget", "color", "fabric", "occasion", "category"):
        value = filters.get(key)
        if value:
            profile[key] = value


def _next_question(profile: Dict[str, Any]) -> Optional[str]:
    """Return the next clarifying question, or ``None`` when enough is known.

    We consider the flow ready once we know both an occasion and a budget; other
    preferences are optional refinements.
    """
    questions = {
        "occasion": (
            "What's the occasion? (e.g. wedding, festive, party, office, casual)"
        ),
        "max_budget": "What's your budget? For example, 'under 5000' or '2000 to 8000'.",
    }
    for key, question in questions.items():
        if not profile.get(key) and key not in profile["asked"]:
            profile["asked"].append(key)
            return question
    # If both were asked but budget is still missing, we proceed with occasion only
    # once we've asked; avoid looping forever.
    if not profile.get("occasion"):
        return questions["occasion"]
    if not profile.get("max_budget") and "max_budget" not in profile["asked"]:
        profile["asked"].append("max_budget")
        return questions["max_budget"]
    return None


def _ready(profile: Dict[str, Any]) -> bool:
    """True when we know enough to run a product search."""
    if not profile.get("occasion"):
        return False
    # Budget is desirable but not mandatory once we've already asked for it.
    if not profile.get("max_budget") and "max_budget" not in profile["asked"]:
        return False
    return True


def _build_query(profile: Dict[str, Any]) -> str:
    """Assemble a free-text query for ``search_and_rank`` from the profile."""
    parts: List[str] = []
    for key in ("color", "fabric", "category", "occasion"):
        value = profile.get(key)
        if value:
            parts.append(str(value))
    if profile.get("max_budget"):
        parts.append(f"under {profile['max_budget']}")
    if profile.get("min_budget"):
        parts.append(f"above {profile['min_budget']}")
    return " ".join(parts).strip() or "ethnic wear"


def _summarize(products: List[Any], profile: Dict[str, Any]) -> str:
    """Render a concise, WhatsApp-friendly summary of ranked products."""
    lines: List[str] = ["Here are a few picks I found for you:"]
    for idx, product in enumerate(products, start=1):
        title = getattr(product, "title", "Product")
        currency = getattr(product, "currency", "INR")
        price = getattr(product, "price", "")
        symbol = _currency_symbol(currency)
        line = f"{idx}. {title} — {symbol}{price}"
        url = getattr(product, "url", "")
        if url:
            line += f"\n   {url}"
        lines.append(line)

    try:
        from commerce.stylist import style_note

        tip = style_note(
            product_name=getattr(products[0], "title", None),
            occasion=profile.get("occasion"),
        )
        if tip:
            lines.append(f"\nStylist tip: {tip}")
    except Exception as exc:  # noqa: BLE001 - tip is best-effort only
        logger.debug("SHOPPER | style note skipped: %s", exc)

    return "\n".join(lines)


def _currency_symbol(code: str) -> str:
    """Best-effort currency symbol lookup (falls back to the raw code)."""
    try:
        from shopify.search import currency_symbol

        return currency_symbol(code)
    except Exception:  # noqa: BLE001
        return (code or "") + " "


def advise(wa_number: str, text: str) -> str:
    """Advance the guided-shopping conversation for a customer.

    Captures preferences from ``text``, asks the next clarifying question when
    something important is missing, and otherwise runs a verified product search
    and returns a friendly, styled summary.

    Args:
        wa_number: The customer's WhatsApp number (the session key).
        text: The customer's latest free-form message.

    Returns:
        A concise, WhatsApp-friendly reply: either a clarifying question, a
        product summary with a styling tip, or a helpful fallback message.
    """
    try:
        profile = _get_profile(wa_number)
        _merge_filters(profile, text)

        if not _ready(profile):
            question = _next_question(profile)
            if question is not None:
                return question

        query = _build_query(profile)
        products: List[Any] = []
        try:
            from shopify.search import search_and_rank

            products = search_and_rank(query, limit=_MAX_RESULTS) or []
        except Exception as exc:  # noqa: BLE001 - search must never break the flow
            logger.debug("SHOPPER | search_and_rank failed: %s", exc)
            products = []

        profile["searched"] = True

        if not products:
            occ = profile.get("occasion") or "your occasion"
            return (
                f"I couldn't find matching products for {occ} right now. "
                "Try widening your budget or a different colour, and I'll look again. "
                "You can also browse our full catalogue any time."
            )
        return _summarize(products, profile)
    except Exception as exc:  # noqa: BLE001 - never raise to the WhatsApp layer
        logger.error("SHOPPER | advise failed for %s: %s", wa_number, exc)
        return (
            "I'm having trouble putting together suggestions right now. "
            "Please tell me the occasion and your budget and I'll try again."
        )


def reset(wa_number: str) -> None:
    """Forget a customer's shopping profile so the flow starts fresh.

    Args:
        wa_number: The customer's WhatsApp number.
    """
    with _LOCK:
        _PROFILES.pop(wa_number, None)
