"""
tests/test_v9_visual.py
------------------------
Deterministic, fully-offline tests for the v9.0 visual product search.

Solid-colour PNGs are generated in-memory with Pillow so the colour-histogram
embedder has an unambiguous signal: a bright-red saree and a bright-blue saree
are indexed, then a *new* mostly-red query image is searched — the top result
must be the red saree. No Shopify, no Gemini, no network.
"""

from __future__ import annotations

from io import BytesIO

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

from PIL import Image  # noqa: E402

import commerce  # noqa: E402
from commerce import visual_search  # noqa: E402


def _solid_png(rgb, size=(96, 96)) -> bytes:
    """Return PNG bytes of a solid-colour image."""
    buf = BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


def _mostly_red_png(size=(96, 96)) -> bytes:
    """Return PNG bytes that are predominantly red with a small blue corner."""
    img = Image.new("RGB", size, (235, 20, 25))
    # Small non-red patch so it isn't identical to the indexed red image.
    corner = Image.new("RGB", (size[0] // 6, size[1] // 6), (40, 40, 220))
    img.paste(corner, (0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_db():
    """Ensure the commerce schema (incl. product_visual_index) exists."""
    commerce.bootstrap()
    yield


def test_compute_features_returns_descriptor():
    """compute_features yields a histogram + perceptual hash descriptor."""
    features = visual_search.compute_features(_solid_png((255, 0, 0)))
    assert isinstance(features, dict)
    assert features["embedder"] == "histogram"
    assert isinstance(features["hist"], list) and len(features["hist"]) == 64
    assert isinstance(features["phash"], str) and features["phash"]
    assert isinstance(features["dominant"], str) and len(features["dominant"]) == 6


def test_compute_features_bad_bytes_returns_none():
    """Undecodable bytes never raise; they return None."""
    assert visual_search.compute_features(b"not-an-image") is None
    assert visual_search.compute_features(b"") is None


def test_index_and_search_by_image_ranks_red_first():
    """Indexing red + blue, then searching a mostly-red image, returns red top."""
    visual_search.index_product(
        "RED-SAREE",
        image_bytes=_solid_png((255, 0, 0)),
        product_name="Red Silk Saree",
        product_type="Sarees",
        color="red",
        price=2999.0,
        url="https://example.test/red-saree",
    )
    visual_search.index_product(
        "BLUE-SAREE",
        image_bytes=_solid_png((0, 0, 255)),
        product_name="Blue Silk Saree",
        product_type="Sarees",
        color="blue",
        price=3499.0,
        url="https://example.test/blue-saree",
    )

    assert visual_search.index_size() >= 2

    results = visual_search.search_by_image(_mostly_red_png(), top_k=5)
    assert results, "expected at least one visual-search result"
    retailer_ids = [r["product_retailer_id"] for r in results]
    assert "RED-SAREE" in retailer_ids
    assert results[0]["product_retailer_id"] == "RED-SAREE"
    # Red should score strictly higher than blue.
    scores = {r["product_retailer_id"]: r["score"] for r in results}
    assert scores["RED-SAREE"] > scores.get("BLUE-SAREE", -1.0)


def test_index_product_without_image_stores_metadata_only():
    """Indexing with no image stores metadata and null features (no raise)."""
    row = visual_search.index_product(
        "META-ONLY",
        product_name="Metadata Only",
        product_type="Sarees",
    )
    assert row["product_retailer_id"] == "META-ONLY"
    assert row["features"] is None
