"""
commerce/visual_search.py
--------------------------
Offline-first **visual product search** for ME-HAAT Fashion AI Bot v9.0.

A query image (a photo a customer sends on WhatsApp, or an upload in the admin
console) is reduced to a compact, JSON-serializable feature descriptor and
matched against a persisted index of product images
(:class:`~database.models.ProductVisualIndex`).

The default embedder is a pure-``numpy`` **colour histogram + perceptual hash**
that needs no network and no model download, so visual search always works
offline and deterministically. When ``config.visual_embedder == "gemini"`` and a
key is configured, a best-effort Gemini-Vision *description* pass is attempted
first; any failure silently falls back to the histogram path.

Design contract: **no public function ever raises.** Failures degrade to
``None`` / ``[]`` / a metadata-only row so the WhatsApp and API layers never have
to guard against exceptions from this module.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import numpy as np

from config import config
from utils.logging import logger

# Histogram configuration: 4 bins per RGB channel -> 4*4*4 = 64-dim descriptor.
_BINS_PER_CHANNEL = 4
_HIST_DIM = _BINS_PER_CHANNEL ** 3
_WORK_SIZE = 64          # colour-histogram working resolution (WORK_SIZE x WORK_SIZE)
_HASH_SIZE = 8           # average-hash grid (8x8 -> 64-bit perceptual hash)

# Blend weights for the final similarity score. The histogram dominates because
# it is the discriminative signal for colour-driven ethnic wear; the perceptual
# hash contributes shape/structure similarity.
_HIST_WEIGHT = 0.7
_PHASH_WEIGHT = 0.3


# --------------------------------------------------------------------------
# Feature extraction
# --------------------------------------------------------------------------

def compute_features(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Reduce raw image bytes to a compact visual feature descriptor.

    The image is opened with Pillow, converted to RGB and resized, then two
    complementary descriptors are computed:

    * ``hist`` — a normalized 64-dim colour histogram (4 bins per channel).
    * ``phash`` — an 8x8 average-hash perceptual hash rendered as a 16-char
      hex string (64 bits).
    * ``dominant`` — the mean colour as an ``rrggbb`` hex string.

    Args:
        image_bytes: The raw bytes of a JPEG/PNG/etc. image.

    Returns:
        A dict ``{"embedder": "histogram", "hist": [...], "phash": "<hex>",
        "dominant": "<rrggbb>"}`` or ``None`` if the image cannot be decoded.
    """
    if not image_bytes:
        return None
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as img:
            rgb = img.convert("RGB").resize((_WORK_SIZE, _WORK_SIZE))
            arr = np.asarray(rgb, dtype=np.float64)  # shape (H, W, 3)

        hist = _color_histogram(arr)
        phash = _average_hash(arr)
        dominant = _dominant_hex(arr)
        return {
            "embedder": "histogram",
            "hist": hist,
            "phash": phash,
            "dominant": dominant,
        }
    except Exception as exc:  # noqa: BLE001 - never raise on a bad image
        logger.debug("VISUAL | compute_features failed: %s", exc)
        return None


def _color_histogram(arr: np.ndarray) -> List[float]:
    """Return a normalized 64-dim colour histogram from an ``(H, W, 3)`` array."""
    # Map each channel value 0..255 into 0.._BINS_PER_CHANNEL-1.
    bins = np.clip((arr / (256.0 / _BINS_PER_CHANNEL)).astype(np.int64),
                   0, _BINS_PER_CHANNEL - 1)
    flat = bins.reshape(-1, 3)
    idx = (flat[:, 0] * _BINS_PER_CHANNEL ** 2
           + flat[:, 1] * _BINS_PER_CHANNEL
           + flat[:, 2])
    counts = np.bincount(idx, minlength=_HIST_DIM).astype(np.float64)
    total = counts.sum()
    if total > 0:
        counts /= total
    return counts.tolist()


def _average_hash(arr: np.ndarray) -> str:
    """Compute an 8x8 average-hash perceptual hash and render it as hex."""
    from PIL import Image

    gray = Image.fromarray(arr.astype(np.uint8), mode="RGB").convert("L").resize(
        (_HASH_SIZE, _HASH_SIZE)
    )
    small = np.asarray(gray, dtype=np.float64)
    avg = small.mean()
    bits = (small > avg).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    width = (_HASH_SIZE * _HASH_SIZE + 3) // 4  # hex chars needed for 64 bits
    return f"{value:0{width}x}"


def _dominant_hex(arr: np.ndarray) -> str:
    """Return the mean colour of the image as an ``rrggbb`` hex string."""
    mean = arr.reshape(-1, 3).mean(axis=0)
    r, g, b = (int(round(float(c))) for c in mean)
    return f"{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------------------
# Similarity scoring
# --------------------------------------------------------------------------

def _hist_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two histograms, clamped to ``[0, 1]``."""
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    if va.shape != vb.shape or va.size == 0:
        return 0.0
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    sim = float(np.dot(va, vb) / (na * nb))
    return max(0.0, min(1.0, sim))


def _phash_similarity(a: str, b: str) -> float:
    """Perceptual-hash Hamming similarity in ``[0, 1]`` (1.0 == identical)."""
    if not a or not b:
        return 0.0
    try:
        ia = int(a, 16)
        ib = int(b, 16)
    except (TypeError, ValueError):
        return 0.0
    bit_count = _HASH_SIZE * _HASH_SIZE
    hamming = bin(ia ^ ib).count("1")
    return 1.0 - (hamming / float(bit_count))


def _score(query: Dict[str, Any], candidate: Dict[str, Any]) -> float:
    """Blend histogram + perceptual-hash similarity into a single score."""
    hist = _hist_similarity(query.get("hist") or [], candidate.get("hist") or [])
    phash = _phash_similarity(query.get("phash") or "", candidate.get("phash") or "")
    return _HIST_WEIGHT * hist + _PHASH_WEIGHT * phash


# --------------------------------------------------------------------------
# Persistence helpers
# --------------------------------------------------------------------------

def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Serialize a ``ProductVisualIndex`` ORM row into a plain dict."""
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "product_retailer_id": row.product_retailer_id,
        "product_name": row.product_name,
        "product_type": row.product_type,
        "color": row.color,
        "price": float(row.price) if row.price is not None else None,
        "image_url": row.image_url,
        "url": row.url,
        "embedder": row.embedder,
        "features": _load_features(row.features),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _load_features(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a stored JSON feature descriptor; return ``None`` on any problem."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def index_product(
    product_retailer_id: str,
    *,
    image_bytes: Optional[bytes] = None,
    product_name: Optional[str] = None,
    product_type: Optional[str] = None,
    color: Optional[str] = None,
    price: Optional[float] = None,
    image_url: Optional[str] = None,
    url: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Upsert a product into the visual index.

    Features are computed from ``image_bytes`` when provided; otherwise the row
    is stored with metadata only and null features (so it can be re-indexed
    later when an image becomes available).

    Args:
        product_retailer_id: Stable per-tenant product identifier (unique key).
        image_bytes: Optional raw product image used to compute features.
        product_name: Human-readable product name.
        product_type: Category / product type (e.g. ``"Sarees"``).
        color: Dominant/marketing colour.
        price: Product price.
        image_url: URL of the product image.
        url: Product detail URL.
        tenant_id: Owning tenant (``None`` for single-store deployments).

    Returns:
        The upserted row as a dict (see :func:`_row_to_dict`). On persistence
        failure an in-memory dict of the same shape is returned instead.
    """
    features = compute_features(image_bytes) if image_bytes else None
    features_json = json.dumps(features) if features else None
    embedder = (features or {}).get("embedder", config.visual_embedder or "histogram")

    try:
        from database.db import session_scope
        from database.models import ProductVisualIndex

        with session_scope() as session:
            row = (
                session.query(ProductVisualIndex)
                .filter_by(tenant_id=tenant_id, product_retailer_id=product_retailer_id)
                .first()
            )
            if row is None:
                row = ProductVisualIndex(
                    tenant_id=tenant_id,
                    product_retailer_id=product_retailer_id,
                )
                session.add(row)
            if product_name is not None:
                row.product_name = product_name
            if product_type is not None:
                row.product_type = product_type
            if color is not None:
                row.color = color
            if price is not None:
                row.price = price
            if image_url is not None:
                row.image_url = image_url
            if url is not None:
                row.url = url
            row.embedder = embedder
            if features_json is not None:
                row.features = features_json
            session.flush()
            result = _row_to_dict(row)
        logger.info(
            "VISUAL | indexed product=%s tenant=%s features=%s",
            product_retailer_id, tenant_id, "yes" if features else "no",
        )
        return result
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.error("VISUAL | index_product failed for %s: %s", product_retailer_id, exc)
        return {
            "id": None,
            "tenant_id": tenant_id,
            "product_retailer_id": product_retailer_id,
            "product_name": product_name,
            "product_type": product_type,
            "color": color,
            "price": price,
            "image_url": image_url,
            "url": url,
            "embedder": embedder,
            "features": features,
            "updated_at": None,
        }


def search_by_features(
    features: Dict[str, Any],
    *,
    top_k: int = 5,
    tenant_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Rank indexed products by visual similarity to a query feature descriptor.

    Args:
        features: A descriptor produced by :func:`compute_features`.
        top_k: Maximum number of results to return.
        tenant_id: Restrict the search to a single tenant when provided.

    Returns:
        A list of ``{"product_retailer_id", "product_name", "price", "url",
        "score"}`` dicts, most similar first. Empty on any failure or when the
        index is empty.
    """
    if not features or not isinstance(features, dict):
        return []
    try:
        from database.db import session_scope
        from database.models import ProductVisualIndex

        scored: List[Dict[str, Any]] = []
        with session_scope() as session:
            query = session.query(ProductVisualIndex)
            if tenant_id is not None:
                query = query.filter(ProductVisualIndex.tenant_id == tenant_id)
            for row in query.all():
                cand = _load_features(row.features)
                if not cand:
                    continue
                scored.append(
                    {
                        "product_retailer_id": row.product_retailer_id,
                        "product_name": row.product_name,
                        "price": float(row.price) if row.price is not None else None,
                        "url": row.url,
                        "score": round(_score(features, cand), 6),
                    }
                )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[: max(0, top_k)]
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.error("VISUAL | search_by_features failed: %s", exc)
        return []


def search_by_image(
    image_bytes: bytes,
    *,
    top_k: int = 5,
    tenant_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Find the products most visually similar to a query image.

    Computes features for ``image_bytes`` and delegates to
    :func:`search_by_features`. When ``config.visual_embedder == "gemini"`` and
    a key is configured, a best-effort Gemini-Vision description pass runs first
    for logging/enrichment; the histogram path always drives the actual match so
    the search works offline regardless of the Gemini result.

    Args:
        image_bytes: Raw query image bytes.
        top_k: Maximum number of results to return.
        tenant_id: Restrict the search to a single tenant when provided.

    Returns:
        Ranked results (see :func:`search_by_features`), or ``[]`` on failure.
    """
    if (config.visual_embedder or "").lower() == "gemini" and config.gemini_api_key:
        description = _gemini_describe(image_bytes)
        if description:
            logger.info("VISUAL | gemini description: %s", description[:120])
        else:
            logger.debug("VISUAL | gemini description unavailable; using histogram")

    features = compute_features(image_bytes)
    if not features:
        return []
    return search_by_features(features, top_k=top_k, tenant_id=tenant_id)


def _gemini_describe(image_bytes: bytes) -> Optional[str]:
    """Best-effort Gemini-Vision one-line description of a product image.

    Fully guarded: returns ``None`` on missing key, network error, or any
    unexpected response shape so the caller can fall back to the histogram path.

    Args:
        image_bytes: Raw image bytes to describe.

    Returns:
        A short natural-language description, or ``None`` on any failure.
    """
    if not image_bytes or not config.gemini_api_key:
        return None
    try:
        import base64

        import requests

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.gemini_vision_model}:generateContent"
        )
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "Describe this ethnic-wear product in one short "
                                "phrase (garment type and dominant colour)."
                            )
                        },
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 64},
        }
        resp = requests.post(
            url,
            params={"key": config.gemini_api_key},
            json=body,
            timeout=getattr(config, "request_timeout_seconds", 15),
        )
        if resp.status_code != 200:
            logger.debug("VISUAL | gemini vision status %s", resp.status_code)
            return None
        data = resp.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - never raise; fall back to histogram
        logger.debug("VISUAL | gemini describe failed: %s", exc)
        return None


def index_size(tenant_id: Optional[int] = None) -> int:
    """Return the number of products currently in the visual index.

    Args:
        tenant_id: Count only a single tenant's rows when provided.

    Returns:
        The row count, or ``0`` on any failure.
    """
    try:
        from database.db import session_scope
        from database.models import ProductVisualIndex

        with session_scope() as session:
            query = session.query(ProductVisualIndex)
            if tenant_id is not None:
                query = query.filter(ProductVisualIndex.tenant_id == tenant_id)
            return int(query.count())
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.error("VISUAL | index_size failed: %s", exc)
        return 0
