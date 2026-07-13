"""
tests/test_v9_stylist.py
-------------------------
Deterministic, offline tests for the v9.0 AI Stylist knowledge base.

No Gemini key is configured in the test environment, so ``style_note`` falls
back to its templated tip — the assertions below never touch the network.
"""

from __future__ import annotations

from commerce import stylist


def test_complete_the_look_saree_includes_blouse():
    """A saree anchor suggests a blouse (and other pieces)."""
    look = stylist.complete_the_look(product_type="saree")
    assert look["anchor"] == "saree"
    categories = [s["category"].lower() for s in look["suggestions"]]
    assert any("blouse" in c for c in categories)
    assert look["colors"]
    assert isinstance(look["note"], str) and look["note"]


def test_complete_the_look_with_color_pairs():
    """A colour anchor surfaces complementary colour pairings."""
    look = stylist.complete_the_look(product_type="saree", color="red")
    assert "gold" in [c.lower() for c in look["colors"]]


def test_complete_the_look_unknown_type_is_safe():
    """An unknown product type returns generic, non-empty suggestions."""
    look = stylist.complete_the_look(product_type="mystery-garment")
    assert look["suggestions"]
    assert look["note"]


def test_suggest_for_occasion_wedding_has_categories():
    """The wedding occasion returns recommended categories, fabrics, colours."""
    guide = stylist.suggest_for_occasion("wedding")
    assert guide["occasion"] == "wedding"
    assert guide["categories"]
    assert guide["fabrics"]
    assert guide["colors"]
    assert guide["note"]


def test_suggest_for_occasion_alias_and_unknown():
    """Aliases resolve; unknown occasions fall back gracefully."""
    assert stylist.suggest_for_occasion("bridal")["occasion"] == "wedding"
    fallback = stylist.suggest_for_occasion("moon-picnic")
    assert fallback["categories"]


def test_style_note_returns_non_empty_string():
    """style_note always returns a non-empty templated tip offline."""
    note = stylist.style_note(product_name="Red Banarasi Saree", occasion="wedding")
    assert isinstance(note, str) and note.strip()
    plain = stylist.style_note()
    assert isinstance(plain, str) and plain.strip()
