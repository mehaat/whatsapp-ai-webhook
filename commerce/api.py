"""
commerce/api.py
----------------
The v6.0 JSON order / tracking API and payment-webhook surface for the
ME-HAAT Fashion AI Bot.

This module defines a single Flask blueprint, :data:`commerce_api_bp`, with
**absolute** route paths (no ``url_prefix``) so the host application can
register it directly:

    from commerce.api import commerce_api_bp
    app.register_blueprint(commerce_api_bp)

Endpoints
    * ``POST /api/token``               — issue a JWT from admin credentials.
    * ``GET  /orders``                  — list orders (auth required).
    * ``GET  /orders/<ref>``            — fetch one order (auth required).
    * ``POST /orders/update``           — mutate an order (auth required).
    * ``GET  /tracking/<ref>``          — public tracking view.
    * ``POST /payments/webhook/<prov>`` — public provider webhook sink.

Every handler is wrapped in defensive error handling: unexpected failures are
logged and returned as JSON with an appropriate status code — stack traces are
never leaked to clients.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, Response, jsonify, request

from admin.security import verify_password, verify_username
from commerce.auth import issue_token, require_api_auth, token_ttl_seconds
from commerce.service import order_service
from utils.logging import logger

commerce_api_bp = Blueprint("commerce_api", __name__)

# Canonical fulfilment pipeline surfaced by the public tracking endpoint.
_TRACKING_STAGES = [
    "received",
    "confirmed",
    "packed",
    "shipped",
    "out_for_delivery",
    "delivered",
]

# Fields a caller may update through ``POST /orders/update`` (besides status
# and the tracking-carrying fields handled by ``set_status``).
_UPDATABLE_FIELDS = (
    "customer_name",
    "city",
    "state",
    "notes",
    "discount",
    "shipping",
    "tax",
    "courier",
    "tracking_number",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _client_ip() -> str:
    """Best-effort client IP, honouring a single proxy hop."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _ref_lookup_kwargs(ref: str) -> Dict[str, Any]:
    """Translate a path ``ref`` into ``get_order`` keyword arguments.

    All-digit refs are treated as the integer primary key; anything else is
    treated as the string ``order_number``.
    """
    ref = (ref or "").strip()
    if ref.isdigit():
        return {"order_id": int(ref)}
    return {"order_number": ref}


def _int_arg(name: str, default: int, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    """Read a bounded integer query parameter with a safe fallback."""
    raw = request.args.get(name, "")
    try:
        value = int(raw) if str(raw).strip() else default
    except (TypeError, ValueError):
        value = default
    if value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _build_stages(order: Dict[str, Any], tracking: list) -> list:
    """Return the canonical pipeline annotated with done/current/pending.

    A stage is ``current`` when it matches the order's live status, ``done``
    when it precedes the live status or appears in the tracking history, and
    ``pending`` otherwise.
    """
    history = {str(evt.get("status", "")).strip().lower() for evt in (tracking or [])}
    current_status = str(order.get("status", "") or "").strip().lower()
    current_idx = _TRACKING_STAGES.index(current_status) if current_status in _TRACKING_STAGES else -1

    stages = []
    for idx, name in enumerate(_TRACKING_STAGES):
        if current_idx >= 0 and idx < current_idx:
            state = "done"
        elif current_idx >= 0 and idx == current_idx:
            state = "current"
        elif name in history:
            state = "done"
        else:
            state = "pending"
        stages.append({"stage": name, "state": state})
    return stages


def _error(message: str, status: int) -> Tuple[Response, int]:
    """Return a JSON error body with the given HTTP status."""
    return jsonify({"error": message}), status


# --------------------------------------------------------------------------
# Token issuance
# --------------------------------------------------------------------------

@commerce_api_bp.route("/api/token", methods=["POST"])
def api_token() -> Tuple[Response, int]:
    """Authenticate admin credentials and mint a bearer JWT.

    Body: ``{"username": ..., "password": ...}``. Returns
    ``{"token": ..., "expires_in": <seconds>}`` on success, or ``401``.
    """
    try:
        body = request.get_json(silent=True) or {}
        username = str(body.get("username", "") or "")
        password = str(body.get("password", "") or "")

        if not verify_username(username) or not verify_password(password):
            logger.info("API | token request rejected for user=%r from %s", username, _client_ip())
            return _error("invalid_credentials", 401)

        token = issue_token(username)
        if not token:
            logger.error("API | token issuance failed (no signing secret configured)")
            return _error("token_unavailable", 503)

        return jsonify({"token": token, "expires_in": token_ttl_seconds()}), 200
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace
        logger.error("API | /api/token failed: %s", exc)
        return _error("internal_error", 500)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

@commerce_api_bp.route("/orders", methods=["GET"])
@require_api_auth
def list_orders() -> Tuple[Response, int]:
    """List orders matching the query filters.

    Query params: ``status``, ``payment_status``, ``q`` (free text),
    ``date_from``, ``date_to``, ``limit`` (default 50, max 200), ``offset``.
    """
    try:
        filters = {
            "status": request.args.get("status") or None,
            "payment_status": request.args.get("payment_status") or None,
            "query": request.args.get("q") or None,
            "date_from": request.args.get("date_from") or None,
            "date_to": request.args.get("date_to") or None,
        }
        limit = _int_arg("limit", 50, minimum=1, maximum=200)
        offset = _int_arg("offset", 0, minimum=0)

        count = order_service.count_orders(**filters)
        results = order_service.list_orders(limit=limit, offset=offset, **filters)
        return jsonify({"count": count, "results": results}), 200
    except Exception as exc:  # noqa: BLE001
        logger.error("API | GET /orders failed: %s", exc)
        return _error("internal_error", 500)


@commerce_api_bp.route("/orders/<ref>", methods=["GET"])
@require_api_auth
def get_order(ref: str) -> Tuple[Response, int]:
    """Fetch a single order (by id or order_number) with items + tracking."""
    try:
        order = order_service.get_order(
            include_items=True, include_tracking=True, **_ref_lookup_kwargs(ref)
        )
        if order is None:
            return _error("not_found", 404)
        return jsonify(order), 200
    except Exception as exc:  # noqa: BLE001
        logger.error("API | GET /orders/%s failed: %s", ref, exc)
        return _error("internal_error", 500)


@commerce_api_bp.route("/orders/update", methods=["POST"])
@require_api_auth
def update_order() -> Tuple[Response, int]:
    """Update an order's status and/or editable fields.

    Body must include ``order`` (id or order_number). Optional ``status`` (with
    ``courier``/``tracking_number``/``location``/``note``) drives a status
    transition; any of the whitelisted editable fields drive a field update.
    Returns the refreshed order dict.
    """
    try:
        body = request.get_json(silent=True) or {}
        ref = body.get("order")
        if ref is None or str(ref).strip() == "":
            return _error("missing_order_ref", 400)

        existing = order_service.get_order(include_items=False, **_ref_lookup_kwargs(str(ref)))
        if existing is None:
            return _error("not_found", 404)
        order_id = int(existing["id"])
        actor = "api"
        ip = _client_ip()

        status = body.get("status")
        if status:
            updated = order_service.set_status(
                order_id,
                str(status),
                actor=actor,
                courier=body.get("courier"),
                tracking_number=body.get("tracking_number"),
                location=body.get("location"),
                note=body.get("note"),
                ip=ip,
            )
            if updated is None:
                return _error("not_found", 404)

        field_updates = {
            key: body[key] for key in _UPDATABLE_FIELDS if key in body and body[key] is not None
        }
        if field_updates:
            updated = order_service.update_order_fields(order_id, actor=actor, ip=ip, **field_updates)
            if updated is None:
                return _error("not_found", 404)

        result = order_service.get_order(
            order_id=order_id, include_items=True, include_tracking=True
        )
        if result is None:
            return _error("not_found", 404)
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        logger.error("API | POST /orders/update failed: %s", exc)
        return _error("internal_error", 500)


# --------------------------------------------------------------------------
# Public tracking
# --------------------------------------------------------------------------

@commerce_api_bp.route("/tracking/<ref>", methods=["GET"])
def tracking(ref: str) -> Tuple[Response, int]:
    """Public order-tracking view (no authentication).

    Resolves ``ref`` by id or order_number and returns the summarized status,
    the canonical stage pipeline, and the raw tracking events.
    """
    try:
        order = order_service.get_order(
            include_items=False, include_tracking=True, **_ref_lookup_kwargs(ref)
        )
        if order is None:
            return _error("not_found", 404)

        events = order.get("tracking") or []
        payload = {
            "order_number": order.get("order_number"),
            "status": order.get("status"),
            "payment_status": order.get("payment_status"),
            "courier": order.get("courier"),
            "tracking_number": order.get("tracking_number"),
            "stages": _build_stages(order, events),
            "tracking": events,
        }
        return jsonify(payload), 200
    except Exception as exc:  # noqa: BLE001
        logger.error("API | GET /tracking/%s failed: %s", ref, exc)
        return _error("internal_error", 500)


# --------------------------------------------------------------------------
# Payment webhooks
# --------------------------------------------------------------------------

@commerce_api_bp.route("/payments/webhook/<provider>", methods=["POST"])
def payments_webhook(provider: str) -> Tuple[Response, int]:
    """Public payment-provider webhook sink.

    Providers cannot present our JWT, so this endpoint is unauthenticated;
    each provider adapter is responsible for verifying its own signature. The
    endpoint always acks with HTTP 200 (even on verification failure) so the
    provider does not enter a retry storm; failures are logged.
    """
    try:
        raw_body = request.get_data() or b""
        headers = dict(request.headers)
        # Lazy import to avoid a commerce <-> payments import cycle at module load.
        from payments import handle_webhook

        result = handle_webhook(provider, headers, raw_body)
        if not result.get("ok"):
            logger.warning(
                "API | webhook for %s not ok (status=%r)", provider, result.get("status")
            )
        return jsonify({"ok": bool(result.get("ok")), "status": result.get("status", "")}), 200
    except Exception as exc:  # noqa: BLE001 - webhooks must always be acked
        logger.error("API | webhook for %s failed: %s", provider, exc)
        return jsonify({"ok": False, "status": ""}), 200
