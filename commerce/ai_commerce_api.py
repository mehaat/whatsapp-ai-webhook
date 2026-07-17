"""
commerce/ai_commerce_api.py
----------------------------
Public JSON API for the v9.0 Advanced AI Commerce surface.

Exposes three lightweight, guarded endpoints (no URL prefix — they live at the
app root):

    * ``POST /api/visual-search``          — image -> similar products.
    * ``GET  /api/stylist/occasion/<occasion>`` — occasion styling guide.
    * ``GET  /api/stylist/complete``       — "complete the look" suggestions.

The visual-search endpoint is public but size-guarded, and returns ``404`` when
``config.visual_search_enabled`` is off. All handlers are fully guarded and only
ever return JSON.
"""

from __future__ import annotations

from typing import Tuple

from flask import Blueprint, Response, jsonify, request

from config import config
from utils.logging import logger

ai_commerce_api_bp = Blueprint("ai_commerce_api", __name__)

# Reject oversized uploads early (bytes). Keeps the public endpoint cheap.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB


def _read_image_bytes() -> bytes:
    """Extract raw image bytes from a multipart ``image`` field or the body."""
    file = request.files.get("image")
    if file is not None:
        return file.read()
    return request.get_data(cache=False) or b""


@ai_commerce_api_bp.route("/api/visual-search", methods=["POST"])
def visual_search() -> Tuple[Response, int]:
    """Return products visually similar to an uploaded query image.

    Accepts a multipart ``image`` file field or a raw image request body.
    Guarded on size and on ``config.visual_search_enabled``.
    """
    if not config.visual_search_enabled:
        return jsonify({"error": "not_found"}), 404
    try:
        image_bytes = _read_image_bytes()
        if not image_bytes:
            return jsonify({"error": "no_image"}), 400
        if len(image_bytes) > _MAX_IMAGE_BYTES:
            return jsonify({"error": "image_too_large"}), 413

        from commerce.visual_search import search_by_image

        top_k = _safe_int(request.args.get("top_k"), default=5)
        results = search_by_image(image_bytes, top_k=top_k)
        return jsonify({"results": results}), 200
    except Exception as exc:  # noqa: BLE001 - endpoint must never 500
        logger.error("AI-COMMERCE API | visual_search failed: %s", exc)
        return jsonify({"error": "visual_search_failed", "results": []}), 200


@ai_commerce_api_bp.route("/api/stylist/occasion/<occasion>", methods=["GET"])
def stylist_occasion(occasion: str) -> Tuple[Response, int]:
    """Return an occasion styling guide (categories, fabrics, colours, note)."""
    if not config.ai_stylist_enabled:
        return jsonify({"error": "not_found"}), 404
    try:
        from commerce.stylist import suggest_for_occasion

        return jsonify(suggest_for_occasion(occasion)), 200
    except Exception as exc:  # noqa: BLE001 - endpoint must never 500
        logger.error("AI-COMMERCE API | stylist_occasion failed: %s", exc)
        return jsonify({"error": "stylist_failed"}), 200


@ai_commerce_api_bp.route("/api/stylist/complete", methods=["GET"])
def stylist_complete() -> Tuple[Response, int]:
    """Return "complete the look" suggestions from query parameters."""
    if not config.ai_stylist_enabled:
        return jsonify({"error": "not_found"}), 404
    try:
        from commerce.stylist import complete_the_look

        result = complete_the_look(
            product_type=request.args.get("product_type"),
            color=request.args.get("color"),
            occasion=request.args.get("occasion"),
        )
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001 - endpoint must never 500
        logger.error("AI-COMMERCE API | stylist_complete failed: %s", exc)
        return jsonify({"error": "stylist_failed"}), 200


def _safe_int(value: object, *, default: int) -> int:
    """Coerce a query-string value to a positive int, bounded to ``[1, 20]``."""
    try:
        parsed = int(value)  # type: ignore[arg-type]
        return max(1, min(20, parsed))
    except (TypeError, ValueError):
        return default
