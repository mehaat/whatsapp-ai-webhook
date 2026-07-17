"""
tests/test_v6_intent.py
-----------------------
Tests for the v6.0 bilingual intent detection (commerce/intent.py).

Pure logic — no network. Verifies that representative English and
Hindi/Hinglish phrases map to the expected intent, plus the
``is_tracking_intent`` convenience helper.
"""

from __future__ import annotations

import pytest

from commerce.intent import detect_intent, detect_language, is_tracking_intent


@pytest.mark.parametrize(
    "phrase, expected",
    [
        # track_order (English + Hindi)
        ("where is my order", "track_order"),
        ("मेरा ऑर्डर कहां है", "track_order"),
        ("track my order", "track_order"),
        # payment
        ("i want to pay", "payment"),
        ("payment link", "payment"),
        # cancel
        ("cancel my order", "cancel"),
        # refund
        ("i want a refund", "refund"),
        # invoice
        ("send me the invoice", "invoice"),
        # browse_products (English + Hindi)
        ("show me sarees", "browse_products"),
        ("साड़ी दिखाओ", "browse_products"),
        # greeting (English + Hindi)
        ("hello", "greeting"),
        ("namaste", "greeting"),
    ],
)
def test_detect_intent(phrase: str, expected: str) -> None:
    """detect_intent maps representative phrases to the right intent."""
    assert detect_intent(phrase) == expected


def test_is_tracking_intent_true() -> None:
    """is_tracking_intent is True for a tracking phrase."""
    assert is_tracking_intent("track my order") is True


def test_is_tracking_intent_false() -> None:
    """is_tracking_intent is False for a non-tracking phrase."""
    assert is_tracking_intent("hello") is False


def test_unknown_intent() -> None:
    """Empty / meaningless input falls back to 'unknown'."""
    assert detect_intent("") == "unknown"


def test_detect_language() -> None:
    """detect_language returns hindi for Devanagari, english otherwise."""
    assert detect_language("मेरा ऑर्डर कहां है") == "hindi"
    assert detect_language("where is my order") == "english"
