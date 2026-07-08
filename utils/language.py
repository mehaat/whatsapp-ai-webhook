"""
utils/language.py
------------------
Lightweight language detection (Hindi / English / Hinglish) and greeting
utilities used to personalize WhatsApp replies.
"""

from __future__ import annotations

import re

_DEVANAGARI_REGEX = re.compile(r"[\u0900-\u097F]")

_HINGLISH_HINTS = {
    "hai", "hain", "kya", "aap", "kaise", "kitna", "kitne", "chahiye",
    "mujhe", "batao", "sari", "saree", "dijiye", "acha", "theek", "nahi",
    "haan", "bhai", "ji", "kripya", "krna", "karna",
}

_GREETING_WORDS = [
    "hi", "hello", "hey", "namaste", "namaskar",
    "good morning", "good evening", "good afternoon",
    "gm", "ge",
]


def detect_language(text: str) -> str:
    """Detect whether text is Hindi (Devanagari), English, or Hinglish (mixed).

    Returns:
        One of "hindi", "hinglish", "english".
    """
    if not text:
        return "english"

    if _DEVANAGARI_REGEX.search(text):
        return "hindi"

    words = set(re.findall(r"[a-zA-Z]+", text.lower()))
    if words.intersection(_HINGLISH_HINTS):
        return "hinglish"

    return "english"


def is_greeting(text: str) -> bool:
    """Detect whether a message is primarily a greeting."""
    if not text:
        return False
    normalized = text.strip().lower()
    if len(normalized.split()) > 4:
        return False
    return any(word in normalized for word in _GREETING_WORDS)


def build_greeting(customer_name: str, language: str) -> str:
    """Build a personalized greeting in the detected language.

    Args:
        customer_name: WhatsApp profile name of the customer.
        language: One of "hindi", "english", "hinglish".

    Returns:
        A personalized greeting string.
    """
    name = customer_name.strip() if customer_name else ""
    name_part = f" {name} Ji" if name else ""

    if language == "hindi":
        return f"Namaste{name_part}! ME-HAAT Fashion mein aapka swagat hai. Main aapki kaise madad kar sakta hoon?"
    if language == "hinglish":
        return f"Namaste{name_part}! Welcome to ME-HAAT Fashion. Aap kya dhoondh rahe hain aaj?"
    return f"Hello{name_part}! Welcome to ME-HAAT Fashion. How can I help you today?"
