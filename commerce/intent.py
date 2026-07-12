"""
commerce/intent.py
------------------
Bilingual (English + Hindi/Hinglish) rule-based intent detection for the
ME-HAAT Fashion AI Bot v6.0 commerce flow.

The detector uses ordered keyword sets so that *specific* intents (e.g.
``track_order``, ``cancel``, ``refund``) win over more *generic* ones (e.g.
``place_order``, ``browse_products``). Latin keywords are matched on a leading
word boundary (so "order" also matches "orders"); Hindi (Devanagari) keywords
and multi-word phrases are matched as substrings.
"""

from __future__ import annotations

import re
from typing import List, Tuple

try:  # Reuse the shared language detector when available.
    from utils.language import detect_language as _detect_language_util
except Exception:  # noqa: BLE001 - fall back to the local detector
    _detect_language_util = None


_DEVANAGARI_REGEX = re.compile(r"[ऀ-ॿ]")

# Ordered (intent, keywords) pairs. Order matters: earlier entries take
# precedence, so place the most specific intents first.
_INTENT_KEYWORDS: List[Tuple[str, List[str]]] = [
    # --- Escalation / human hand-off (most specific first) ---
    (
        "escalation",
        [
            "complaint", "escalate", "escalation", "manager", "supervisor",
            "shikayat", "शिकायत", "बहुत खराब",
        ],
    ),
    (
        "human_agent",
        [
            "human", "agent", "representative", "real person", "talk to someone",
            "customer care", "insaan", "इंसान", "व्यक्ति", "एजेंट",
        ],
    ),
    # --- Money movement: refund before return, both before generic order ---
    (
        "refund",
        [
            "refund", "money back", "paisa wapas", "paise wapas", "refund status",
            "रिफंड", "पैसा वापस", "पैसे वापस",
        ],
    ),
    (
        "return",
        [
            "return", "exchange", "send it back", "wapas karna",
            "वापसी", "लौटाना", "वापस करना",
        ],
    ),
    (
        "cancel",
        [
            "cancel", "cancellation", "cancel my order", "rad kar",
            "रद्द", "कैंसिल",
        ],
    ),
    # --- Order tracking (before place_order so "track my order" wins) ---
    (
        "track_order",
        [
            "track", "tracking", "where is my order", "where is order",
            "order status", "status of my order", "order kahan", "kahan hai",
            "मेरा ऑर्डर कहां है", "ऑर्डर कहां", "कहां है", "ट्रैक",
        ],
    ),
    # --- Payment ---
    (
        "payment",
        [
            "pay", "payment", "pay now", "payment link", "upi", "checkout",
            "bhugtan", "भुगतान", "पेमेंट", "भुगतान करें",
        ],
    ),
    # --- Invoice / billing ---
    (
        "invoice",
        [
            "invoice", "bill", "receipt", "gst", "tax invoice",
            "बिल", "रसीद", "इनवॉइस",
        ],
    ),
    # --- Coupon / discount ---
    (
        "coupon",
        [
            "coupon", "promo", "promo code", "discount", "offer", "voucher",
            "कूपन", "छूट", "डिस्काउंट",
        ],
    ),
    # --- Stock / availability ---
    (
        "stock",
        [
            "stock", "in stock", "available", "availability", "out of stock",
            "स्टॉक", "उपलब्ध",
        ],
    ),
    # --- Delivery time / ETA ---
    (
        "delivery_time",
        [
            "delivery time", "delivery", "how long", "when will", "eta",
            "kab aayega", "kab aaega", "kab tak", "kitne din",
            "कब आएगा", "कब तक", "डिलीवरी",
        ],
    ),
    # --- Place order (generic; after all order-specific intents) ---
    (
        "place_order",
        [
            "place order", "buy", "purchase", "order now", "i want to order",
            "khareed", "kharidna", "order karna",
            "खरीद", "ऑर्डर करना", "मंगवाना",
        ],
    ),
    # --- Browse / catalog ---
    (
        "browse_products",
        [
            "saree", "sari", "lehenga", "kurti", "suit", "dress", "product",
            "catalog", "catalogue", "show", "dikhao", "dekhna",
            "साड़ी", "दिखाओ", "लहंगा", "कुर्ती", "प्रोडक्ट",
        ],
    ),
    # --- Support (generic help) ---
    (
        "support",
        [
            "help", "support", "problem", "issue", "assist", "query",
            "madad", "मदद", "सहायता", "समस्या",
        ],
    ),
    # --- Greeting (low priority) ---
    (
        "greeting",
        [
            "hello", "hi", "hey", "namaste", "namaskar",
            "good morning", "good evening", "good afternoon",
            "नमस्ते", "नमस्कार",
        ],
    ),
]


def _matches(keyword: str, text: str) -> bool:
    """Return True when ``keyword`` occurs in ``text``.

    Latin single-word keywords are matched on a leading word boundary (so
    "order" also matches "orders"). Devanagari keywords and multi-word phrases
    are matched as plain substrings.
    """
    if _DEVANAGARI_REGEX.search(keyword) or " " in keyword:
        return keyword in text
    return re.search(r"\b" + re.escape(keyword), text) is not None


def detect_intent(text: str) -> str:
    """Detect the customer's intent from a free-text message.

    Args:
        text: The inbound message text (English, Hindi, or Hinglish).

    Returns:
        A lowercase intent string, one of: ``browse_products``,
        ``place_order``, ``track_order``, ``payment``, ``return``, ``refund``,
        ``cancel``, ``delivery_time``, ``invoice``, ``coupon``, ``stock``,
        ``support``, ``human_agent``, ``escalation``, ``greeting`` or
        ``unknown``.
    """
    if not text or not text.strip():
        return "unknown"

    normalized = text.lower()

    for intent, keywords in _INTENT_KEYWORDS:
        for keyword in keywords:
            if _matches(keyword.lower(), normalized):
                return intent

    return "unknown"


def is_tracking_intent(text: str) -> bool:
    """Return True when ``text`` expresses an order-tracking intent.

    Args:
        text: The inbound message text.

    Returns:
        True if the detected intent is ``track_order``, else False.
    """
    return detect_intent(text) == "track_order"


def detect_language(text: str) -> str:
    """Detect the message language as ``"hindi"`` or ``"english"``.

    Reuses :func:`utils.language.detect_language` when available (mapping its
    "hinglish" result to "english"); otherwise applies a small local detector
    that flags any Devanagari text as Hindi.

    Args:
        text: The inbound message text.

    Returns:
        ``"hindi"`` or ``"english"``.
    """
    if _detect_language_util is not None:
        try:
            return "hindi" if _detect_language_util(text) == "hindi" else "english"
        except Exception:  # noqa: BLE001 - fall through to local detector
            pass

    if text and _DEVANAGARI_REGEX.search(text):
        return "hindi"
    return "english"
