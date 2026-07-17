"""
tests/test_v10_1_search.py
--------------------------
v10.1 tests for the enhanced AI query parser in ``shopify/search.py``.

These exercise ``extract_search_filters`` (Hindi + English + Hinglish + common
typos) and ``detect_product_search_intent``. They must pass alongside the
existing ``tests/test_v4_product_flow.py`` suite (no regressions).

Run:  pytest -q
"""

from __future__ import annotations

import pytest

from shopify.search import detect_product_search_intent, extract_search_filters


# --------------------------------------------------------------------------
# Backward-compat: the exact v4 dict-key contract must not change.
# --------------------------------------------------------------------------

def test_v4_contract_preserved():
    f = extract_search_filters("red silk saree under 3000")
    assert set(f.keys()) == {
        "max_budget", "min_budget", "color", "fabric", "occasion", "category",
    }
    assert f["max_budget"] == "3000"
    assert f["color"] == "red"
    assert f["fabric"] == "silk"
    assert f["category"] == "Sarees"

    r = extract_search_filters("saree 2000 to 5000")
    assert r["min_budget"] == "2000" and r["max_budget"] == "5000"


# --------------------------------------------------------------------------
# Price parsing (English + Hindi + Hinglish + bare number).
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected_max",
    [
        ("799 wali saree", "799"),
        ("799 ki saree", "799"),
        ("799 wala", "799"),
        ("under 3000", "3000"),
        ("below 999", "999"),
        ("upto 2000", "2000"),
        ("less than 1500", "1500"),
        ("3000 tak", "3000"),
        ("3000 ke andar", "3000"),
        ("3000 se kam", "3000"),
        ("2000 silk", "2000"),  # bare number + fabric
    ],
)
def test_max_budget_variants(text, expected_max):
    assert extract_search_filters(text)["max_budget"] == expected_max


@pytest.mark.parametrize(
    "text,expected_min",
    [
        ("above 5000", "5000"),
        ("over 2000", "2000"),
        ("5000 se upar", "5000"),
    ],
)
def test_min_budget_variants(text, expected_min):
    assert extract_search_filters(text)["min_budget"] == expected_min


@pytest.mark.parametrize(
    "text",
    ["2000 to 5000", "2000-5000", "between 2000 and 5000"],
)
def test_range_variants(text):
    f = extract_search_filters(text)
    assert f["min_budget"] == "2000" and f["max_budget"] == "5000"


# --------------------------------------------------------------------------
# Spec examples.
# --------------------------------------------------------------------------

def test_spec_799_wali_saree():
    assert extract_search_filters("799 wali saree")["max_budget"] == "799"


def test_spec_red_cotton_saree():
    f = extract_search_filters("Red cotton saree")
    assert f["color"] == "red"
    assert f["fabric"] == "cotton"


def test_spec_banarasi_silk_under_3000():
    f = extract_search_filters("Banarasi silk under 3000")
    assert f["fabric"] == "silk"
    assert f["max_budget"] == "3000"


def test_spec_wedding_saree():
    assert extract_search_filters("wedding saree")["occasion"] == "wedding"


def test_spec_party_wear():
    assert extract_search_filters("party wear")["occasion"] == "party"


def test_spec_green_cotton_under_999():
    f = extract_search_filters("green cotton under 999")
    assert f["color"] == "green"
    assert f["fabric"] == "cotton"
    assert f["max_budget"] == "999"


def test_spec_hinglish_lal_saree_2000_tak():
    f = extract_search_filters("lal saree 2000 tak")
    assert f["color"] == "red"
    assert f["max_budget"] == "2000"


def test_spec_typo_cottn_sari():
    f = extract_search_filters("cottn sari")
    assert f["fabric"] == "cotton"
    assert f["category"] == "Sarees"


# --------------------------------------------------------------------------
# Colour + fabric Hindi/Hinglish/typo aliases.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,color",
    [
        ("neela suit", "blue"),
        ("hara lehenga", "green"),
        ("kala gown", "black"),
        ("safed kurti", "white"),
        ("gulabi dupatta", "pink"),
        ("golden saree", "gold"),
    ],
)
def test_color_aliases(text, color):
    assert extract_search_filters(text)["color"] == color


@pytest.mark.parametrize(
    "text,fabric",
    [
        ("resham saree", "silk"),
        ("suti kurti", "cotton"),
        ("silck saree", "silk"),
        ("weding georgette", "georgette"),
    ],
)
def test_fabric_aliases(text, fabric):
    assert extract_search_filters(text)["fabric"] == fabric


# --------------------------------------------------------------------------
# Occasion aliases.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,occasion",
    [
        ("shaadi ki saree", "wedding"),
        ("vivah lehenga", "wedding"),
        ("weding saree", "wedding"),
        ("party wear saree", "party"),
        ("casual kurti", "casual"),
        ("reception gown", "reception"),
        ("engagement lehenga", "engagement"),
    ],
)
def test_occasion_aliases(text, occasion):
    assert extract_search_filters(text)["occasion"] == occasion


# --------------------------------------------------------------------------
# Category vocabulary.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,category",
    [
        ("show me a lehenga", "Lehengas"),
        ("cotton kurti", "Kurtis"),
        ("salwar suit", "Suits"),
        ("party gown", "Gowns"),
        ("silk dupatta", "Dupattas"),
        ("designer blouse", "Blouses"),
        ("saree", "Sarees"),
    ],
)
def test_category_vocabulary(text, category):
    assert extract_search_filters(text)["category"] == category


# --------------------------------------------------------------------------
# Intent detection: enhanced queries True, greetings/thanks/more False.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "799 wali saree",
        "lal cotton saree",
        "shaadi ki saree",
        "green cotton under 999",
        "cottn sari",
        "resham saree",
        "party wear",
    ],
)
def test_intent_true(text):
    assert detect_product_search_intent(text) is True


@pytest.mark.parametrize("text", ["hello", "thank you", "more", ""])
def test_intent_false(text):
    assert detect_product_search_intent(text) is False


# --------------------------------------------------------------------------
# Purity / robustness: never raises, returns stable keys on odd input.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "!!!", "123", None])
def test_never_raises(text):
    f = extract_search_filters(text)  # type: ignore[arg-type]
    assert set(f.keys()) == {
        "max_budget", "min_budget", "color", "fabric", "occasion", "category",
    }
