"""
ai/faq.py
---------
Verified, hard-coded FAQ answers for ME-HAAT Fashion.

The AI must never invent policy details, so all policy-related answers
(COD, delivery, payment, exchange, return, refund, tracking, timing, care)
live here as ground truth and are injected into the Gemini prompt as
verified context whenever a matching intent is detected.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

FAQ_ANSWERS: Dict[str, str] = {
    "cod": (
        "Cash on Delivery (COD) is available on select pin codes. COD orders may "
        "carry a small additional handling fee, shown at checkout."
    ),
    "delivery": (
        "Standard delivery typically takes 5-8 business days across India. "
        "Delivery timelines may vary by location and product availability."
    ),
    "payment": (
        "We accept UPI, credit/debit cards, net banking, and Cash on Delivery (COD) "
        "on eligible orders via our website checkout."
    ),
    "exchange": (
        "Exchange is available within 7 days of delivery for unused items with original "
        "tags and packaging intact. Please contact support to initiate an exchange."
    ),
    "return": (
        "Returns are accepted within 7 days of delivery for unused items with original "
        "tags and packaging intact. Please contact support to initiate a return."
    ),
    "refund": (
        "Refunds are processed to the original payment method within 5-7 business days "
        "after the returned item passes quality check."
    ),
    "tracking": (
        "Once your order ships, you will receive a tracking link via SMS/WhatsApp/email. "
        "You can also ask me for your order status if you share your order number."
    ),
    "store_timing": (
        "Our online store is open 24/7. Customer support is available Monday to Saturday, "
        "10:00 AM to 7:00 PM IST."
    ),
    "customer_care": (
        "For any issue our AI assistant cannot resolve, please contact ME-HAAT Fashion "
        "customer support through our website for the fastest response."
    ),
}

_FAQ_KEYWORDS: Dict[str, List[str]] = {
    "cod": ["cod", "cash on delivery"],
    "delivery": ["delivery", "shipping", "deliver", "kab tak aayega", "shipping time"],
    "payment": ["payment", "pay", "upi", "card payment", "netbanking"],
    "exchange": ["exchange"],
    "return": ["return", "returns policy"],
    "refund": ["refund", "refund policy", "paisa wapas"],
    "tracking": ["tracking", "track order", "track my order", "order status"],
    "store_timing": ["store timing", "shop timing", "timing", "open time", "kab khulta"],
    "customer_care": ["customer care", "support", "helpline", "contact number"],
}

_CATALOGUE_KEYWORDS = [
    "catalogue", "catalog", "products", "saree", "sarees", "show products",
    "show me", "dikhao", "collection",
]

_ORDER_NUMBER_PATTERN = re.compile(r"#?\d{3,8}")


def match_faq(text: str) -> Optional[Tuple[str, str]]:
    """Match user text against known FAQ intents.

    Args:
        text: Sanitized user message.

    Returns:
        A tuple of (intent_key, verified_answer) if matched, else None.
    """
    if not text:
        return None

    normalized = text.lower()
    for intent, keywords in _FAQ_KEYWORDS.items():
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, normalized):
                return intent, FAQ_ANSWERS[intent]

    return None


def wants_catalogue(text: str) -> bool:
    """Detect whether the user is asking to browse products / the catalogue."""
    if not text:
        return False
    normalized = text.lower()
    return any(kw in normalized for kw in _CATALOGUE_KEYWORDS)


def wants_order_status(text: str) -> bool:
    """Detect whether the user is asking about an order's status."""
    if not text:
        return False
    normalized = text.lower()
    return "order" in normalized and ("status" in normalized or "track" in normalized or _ORDER_NUMBER_PATTERN.search(normalized) is not None)


def extract_order_number(text: str) -> Optional[str]:
    """Extract a likely order number/name from free text, if present."""
    match = _ORDER_NUMBER_PATTERN.search(text or "")
    return match.group(0) if match else None
