"""
commerce/reco_api.py
---------------------
ME-HAAT Fashion AI Bot v9.0 — JSON API for the recommendation engine.

A single additive Flask blueprint (:data:`reco_api_bp`, no ``url_prefix``)
exposing the :mod:`commerce.recommendations` strategies over HTTP. The catalog
and cross-sell surfaces are **public** (trending / FBT / similar) so storefronts
and the WhatsApp catalog can call them without a token; the customer-specific
"for this shopper" endpoint is **protected** with :func:`require_api_auth`.

Every handler is guarded and 404-safe: an unknown product or customer simply
yields ``{"results": []}`` with HTTP 200 (an empty recommendation set is a valid
answer, not an error).
"""

from __future__ import annotations

from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from commerce import recommendations as reco
from commerce.auth import require_api_auth
from utils.logging import logger

reco_api_bp = Blueprint("reco_api", __name__)


def _int_arg(name: str, default: int) -> int:
    """Read a positive integer query arg, falling back to ``default``."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _ok(results: List[Dict[str, Any]]):
    """Wrap a result list in the standard JSON envelope."""
    return jsonify({"results": results or []})


@reco_api_bp.route("/api/recommendations/trending", methods=["GET"])
def trending():
    """Public: best-selling products. Supports ``?limit=`` and ``?days=``."""
    try:
        results = reco.trending(
            limit=_int_arg("limit", 10),
            days=_int_arg("days", 30),
        )
    except Exception as exc:  # noqa: BLE001 - endpoints never 500 on analytics
        logger.debug("RECO API | trending failed: %r", exc)
        results = []
    return _ok(results)


@reco_api_bp.route("/api/recommendations/with/<retailer_id>", methods=["GET"])
def with_product(retailer_id: str):
    """Public: products frequently bought together with ``retailer_id``."""
    try:
        results = reco.frequently_bought_together(
            retailer_id, limit=_int_arg("limit", 5)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("RECO API | with(%s) failed: %r", retailer_id, exc)
        results = []
    return _ok(results)


@reco_api_bp.route("/api/recommendations/for/<wa_number>", methods=["GET"])
@require_api_auth
def for_customer(wa_number: str):
    """Protected: personalized recommendations for a specific customer."""
    try:
        results = reco.personalized(wa_number, limit=_int_arg("limit", 10))
    except Exception as exc:  # noqa: BLE001
        logger.debug("RECO API | for(%s) failed: %r", wa_number, exc)
        results = []
    return _ok(results)


@reco_api_bp.route("/api/recommendations/similar/<retailer_id>", methods=["GET"])
def similar(retailer_id: str):
    """Public: products similar to ``retailer_id`` by attribute overlap."""
    try:
        results = reco.similar_products(retailer_id, limit=_int_arg("limit", 5))
    except Exception as exc:  # noqa: BLE001
        logger.debug("RECO API | similar(%s) failed: %r", retailer_id, exc)
        results = []
    return _ok(results)
