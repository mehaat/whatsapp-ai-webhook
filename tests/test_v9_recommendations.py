"""
tests/test_v9_recommendations.py
---------------------------------
Tests for the v9.0 recommendation engine in :mod:`commerce.recommendations`.

No-network, no-mock: the commerce DB is bootstrapped via
:func:`commerce.bootstrap` and real orders are created through ``order_service``.
Because the default SQLite DB is shared across the suite, assertions use
unique, test-scoped product IDs (namespaced with a random run token) so the
co-occurrence / trending signals under test are isolated from other suites.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

from decimal import Decimal

import pytest

import commerce
from commerce import recommendations as reco
from commerce.schema import ParsedItem, ParsedOrder
from commerce.service import order_service

# A per-run token keeps this module's products distinct from other suites'.
RUN = uuid.uuid4().hex[:8]
SKU_A = f"RECO-A-{RUN}"
SKU_B = f"RECO-B-{RUN}"
SKU_C = f"RECO-C-{RUN}"
SKU_LONE = f"RECO-LONE-{RUN}"


def _unique_wa() -> str:
    return "9198" + f"{uuid.uuid4().int % 10**8:08d}"


def _item(retailer_id: str, name: str, price: str = "500") -> ParsedItem:
    return ParsedItem(
        product_retailer_id=retailer_id,
        quantity=1,
        unit_price=Decimal(price),
        product_name=name,
    )


def _order(wa: str, items: list) -> dict:
    return order_service.create_order(
        ParsedOrder(wa_number=wa, customer_name="Reco Tester", items=items)
    )


@pytest.fixture(scope="module")
def seeded() -> dict:
    """Seed a co-purchase pattern: A+B frequently, A+C occasionally.

    Returns the wa_number of a customer who bought A (for personalization).
    """
    commerce.bootstrap()

    a_buyer = _unique_wa()

    # The personalization subject bought ONLY A (so B is a valid, un-owned
    # cross-sell for them).
    _order(a_buyer, [_item(SKU_A, "Silk Anarkali Kurti")])

    # A co-occurs with B in three orders from other customers (strong signal).
    _order(_unique_wa(), [_item(SKU_A, "Silk Anarkali Kurti"), _item(SKU_B, "Matching Dupatta")])
    _order(_unique_wa(), [_item(SKU_A, "Silk Anarkali Kurti"), _item(SKU_B, "Matching Dupatta")])
    _order(_unique_wa(), [_item(SKU_A, "Silk Anarkali Kurti"), _item(SKU_B, "Matching Dupatta")])

    # A co-occurs with C in just one order (the weak signal).
    _order(_unique_wa(), [_item(SKU_A, "Silk Anarkali Kurti"), _item(SKU_C, "Beaded Clutch")])

    # A lone, unrelated order so trending() has extra breadth.
    _order(_unique_wa(), [_item(SKU_LONE, "Solo Scarf")])

    return {"a_buyer": a_buyer}


def _ids(results: list) -> list:
    return [r["product_retailer_id"] for r in results]


def test_frequently_bought_together_ranks_b_above_c(seeded):
    results = reco.frequently_bought_together(SKU_A, limit=10)
    assert isinstance(results, list)
    ids = _ids(results)
    # The seed item is never recommended to itself.
    assert SKU_A not in ids
    # Both co-purchased items surface...
    assert SKU_B in ids
    assert SKU_C in ids
    # ...and B (3 co-occurrences) outranks C (1 co-occurrence).
    assert ids.index(SKU_B) < ids.index(SKU_C)
    # Result items carry the documented shape.
    top = results[0]
    assert top["product_retailer_id"] == SKU_B
    assert "product_name" in top and "score" in top
    assert top["score"] >= 3.0


def test_trending_returns_ranked_nonempty_list(seeded):
    results = reco.trending(limit=10, days=30)
    assert isinstance(results, list)
    assert results, "expected a non-empty trending list"
    # Scores are monotonically non-increasing (ranked by quantity).
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    # Our heavily-ordered SKU_A should appear among trending products.
    assert SKU_A in _ids(results)


def test_personalized_recommends_b_and_excludes_owned(seeded):
    results = reco.personalized(seeded["a_buyer"], limit=10)
    assert isinstance(results, list)
    ids = _ids(results)
    # B is the strongest co-purchase for a customer who bought A.
    assert SKU_B in ids
    # The customer already owns A — never recommend owned items back.
    assert SKU_A not in ids


def test_personalized_cold_start_falls_back_to_trending(seeded):
    # A brand-new customer with no history gets the trending list.
    results = reco.personalized(_unique_wa(), limit=5)
    assert isinstance(results, list)
    # Non-empty because the module seeded trending activity.
    assert results


def test_similar_products_returns_list(seeded):
    results = reco.similar_products(SKU_A, limit=5)
    assert isinstance(results, list)
    # Every entry has the documented keys.
    for r in results:
        assert "product_retailer_id" in r
        assert "product_name" in r
        assert "score" in r


def test_recommend_for_whatsapp_compact_shape(seeded):
    results = reco.recommend_for_whatsapp(seeded["a_buyer"], limit=3)
    assert isinstance(results, list)
    assert len(results) <= 3
    for r in results:
        assert set(r.keys()) <= {"product_retailer_id", "product_name", "score", "price"}
        assert "product_retailer_id" in r
