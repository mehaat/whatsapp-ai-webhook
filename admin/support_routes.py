"""
admin/support_routes.py
-----------------------
Flask blueprint for the v10.2 real-time WhatsApp Support Console.

Mounted at ``/admin/support``. Every route requires an authenticated admin
(``login_required``); every state-changing route enforces CSRF
(``csrf_protect``) and a per-admin rate limit, validates its input, and writes an
audit row. The blueprint is intentionally thin — all logic lives in
:mod:`admin.support_console` (data) and :mod:`whatsapp.support_sender` (sending).

No existing route, template or behaviour is modified; this is purely additive.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request,
    session,
)

from admin import support_console as sc
from admin.security import csrf_protect, login_required
from utils.logging import logger
from utils.ratelimit import RateLimiter

support_bp = Blueprint("admin_support", __name__, url_prefix="/admin/support")

# Per-admin send rate limit (protects the WhatsApp API + prevents abuse).
_send_limiter = RateLimiter(max_requests=40, window_seconds=60)
_action_limiter = RateLimiter(max_requests=120, window_seconds=60)

# Upload constraints (WhatsApp Cloud API media limits, kept conservative).
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024  # 16 MB
_ALLOWED_MIME_PREFIXES = ("image/", "audio/", "video/")
_ALLOWED_DOC_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword",
    "text/plain",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _current_user() -> str:
    return session.get("admin_user") or "admin"


def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")


def _rate_ok(limiter: RateLimiter) -> bool:
    return limiter.is_allowed(f"{_current_user()}::{request.endpoint}")


def _err(message: str, code: int = 400) -> Tuple[Any, int]:
    return jsonify({"ok": False, "error": message}), code


def _valid_wa(wa_number: str) -> bool:
    """Basic E.164-ish validation: 6–20 digits, optionally leading +."""
    if not wa_number:
        return False
    digits = wa_number[1:] if wa_number.startswith("+") else wa_number
    return digits.isdigit() and 6 <= len(digits) <= 20


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
@support_bp.route("/console", methods=["GET"])
@support_bp.route("/", methods=["GET"])
@login_required
def console() -> Any:
    """Render the WhatsApp-style support console shell (data loads via AJAX)."""
    return render_template("admin/support_console.html")


# --------------------------------------------------------------------------- #
# Read APIs (polled every ~3s by the frontend)
# --------------------------------------------------------------------------- #
@support_bp.route("/api/inbox", methods=["GET"])
@login_required
def api_inbox() -> Any:
    search = (request.args.get("q") or "").strip()[:64]
    status_filter = (request.args.get("status") or "").strip()
    only_unread = request.args.get("unread") in ("1", "true", "yes")
    rows = sc.inbox(search=search, status_filter=status_filter, only_unread=only_unread)
    return jsonify({"ok": True, "conversations": rows, "count": len(rows)})


@support_bp.route("/api/thread/<wa_number>", methods=["GET"])
@login_required
def api_thread(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    if request.args.get("mark_read") in ("1", "true", "yes"):
        sc.mark_read(wa_number)
    messages = sc.thread(wa_number)
    return jsonify({"ok": True, "wa_number": wa_number, "messages": messages})


@support_bp.route("/api/profile/<wa_number>", methods=["GET"])
@login_required
def api_profile(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    return jsonify({"ok": True, "profile": sc.customer_profile(wa_number)})


@support_bp.route("/api/stats", methods=["GET"])
@login_required
def api_stats() -> Any:
    return jsonify({"ok": True, "stats": sc.live_stats()})


@support_bp.route("/api/notes/<wa_number>", methods=["GET"])
@login_required
def api_list_notes(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    return jsonify({"ok": True, "notes": sc.list_notes(wa_number)})


# --------------------------------------------------------------------------- #
# Manual reply — text / image / document / voice
# --------------------------------------------------------------------------- #
@support_bp.route("/api/send/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_send(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    if not _rate_ok(_send_limiter):
        return _err("rate limit exceeded, slow down", 429)

    from whatsapp import support_sender as ws

    user = _current_user()
    upload = request.files.get("file")

    # ---- Media send (multipart) ----
    if upload is not None and upload.filename:
        content = upload.read()
        if not content:
            return _err("empty file")
        if len(content) > _MAX_UPLOAD_BYTES:
            return _err("file too large (max 16 MB)", 413)
        mime = upload.mimetype or ws.guess_mime(upload.filename)
        caption = (request.form.get("caption") or "").strip()[:1024]

        if mime.startswith("image/"):
            kind, msg_type = "image", "image"
        elif mime.startswith("audio/"):
            kind, msg_type = "audio", "audio"
        elif mime in _ALLOWED_DOC_MIMES or mime == "application/octet-stream":
            kind, msg_type = "document", "document"
        elif mime.startswith(_ALLOWED_MIME_PREFIXES):
            kind, msg_type = "document", "document"
        else:
            return _err(f"unsupported file type: {mime}")

        wamid, media_id = ws.send_media_upload(
            wa_number, content, mime, kind=kind, filename=upload.filename, caption=caption
        )
        status = "sent" if wamid else "failed"
        rec = sc.record_admin_message(
            wa_number, user, msg_type=msg_type, body=caption or upload.filename,
            media_id=media_id, filename=upload.filename, mime_type=mime,
            wa_message_id=wamid, status=status,
            error=None if wamid else "whatsapp send failed",
        )
        if wamid:
            sc.mark_outbound(wa_number, f"[{msg_type}] {caption or upload.filename}")
        sc.audit(user, "send_media", wa_number, f"{msg_type} {upload.filename} -> {status}", _client_ip())
        return jsonify({"ok": bool(wamid), "message": rec}), (200 if wamid else 502)

    # ---- Text send (JSON or form) ----
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or request.form.get("text") or "").strip()
    if not text:
        return _err("text is required")
    if len(text) > 4096:
        text = text[:4096]

    wamid = ws.send_text(wa_number, text)
    status = "sent" if wamid else "failed"
    rec = sc.record_admin_message(
        wa_number, user, msg_type="text", body=text, wa_message_id=wamid,
        status=status, error=None if wamid else "whatsapp send failed",
    )
    if wamid:
        sc.mark_outbound(wa_number, text)
    sc.audit(user, "send_text", wa_number, f"len={len(text)} -> {status}", _client_ip())
    return jsonify({"ok": bool(wamid), "message": rec}), (200 if wamid else 502)


# --------------------------------------------------------------------------- #
# AI toggle / manual mode
# --------------------------------------------------------------------------- #
@support_bp.route("/api/ai-toggle/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_ai_toggle(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    if not _rate_ok(_action_limiter):
        return _err("rate limit exceeded", 429)
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("ai_enabled", request.form.get("ai_enabled") == "true"))
    result = sc.set_ai_enabled(wa_number, enabled, _current_user())
    sc.audit(_current_user(), "ai_toggle", wa_number,
             f"ai_enabled={enabled}", _client_ip())
    return jsonify({"ok": True, **result})


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #
@support_bp.route("/api/assign/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_assign(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    payload = request.get_json(silent=True) or {}
    assigned_to = (payload.get("assigned_to") or request.form.get("assigned_to") or "").strip() or None
    if assigned_to == "__me__":
        assigned_to = _current_user()
    result = sc.assign(wa_number, assigned_to, _current_user())
    sc.audit(_current_user(), "assign", wa_number, f"assigned_to={assigned_to}", _client_ip())
    return jsonify({"ok": True, **result})


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
@support_bp.route("/api/status/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_set_status(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    payload = request.get_json(silent=True) or {}
    status = (payload.get("status") or request.form.get("status") or "open").strip()
    new_status = sc.set_status(wa_number, status, _current_user())
    sc.audit(_current_user(), "set_status", wa_number, new_status, _client_ip())
    return jsonify({"ok": True, "status": new_status})


# --------------------------------------------------------------------------- #
# Internal notes
# --------------------------------------------------------------------------- #
@support_bp.route("/api/notes/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_add_note(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    payload = request.get_json(silent=True) or {}
    note = (payload.get("note") or request.form.get("note") or "").strip()
    if not note:
        return _err("note is required")
    try:
        created = sc.add_note(wa_number, _current_user(), note)
    except ValueError as exc:
        return _err(str(exc))
    sc.audit(_current_user(), "add_note", wa_number, f"len={len(note)}", _client_ip())
    return jsonify({"ok": True, "note": created})


# --------------------------------------------------------------------------- #
# Shopify — search, product card, draft order
# --------------------------------------------------------------------------- #
@support_bp.route("/api/shopify/search", methods=["GET"])
@login_required
def api_shopify_search() -> Any:
    query = (request.args.get("q") or "").strip()[:120]
    if not query:
        return jsonify({"ok": True, "products": []})
    try:
        from shopify.search import search_products

        matches = search_products(query_text=query, limit=8)
        products = [m.to_card_dict() for m in matches]
        return jsonify({"ok": True, "products": products})
    except Exception as exc:  # noqa: BLE001
        logger.error("SUPPORT | shopify search failed: %s", exc)
        return jsonify({"ok": True, "products": [], "warning": "search unavailable"})


@support_bp.route("/api/shopify/send-card/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_shopify_send_card(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    if not _rate_ok(_send_limiter):
        return _err("rate limit exceeded", 429)
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()[:120]
    if not query:
        return _err("query is required")
    try:
        from shopify.search import search_products
        from whatsapp.sender import send_products

        matches = search_products(query_text=query, limit=3)
        if not matches:
            return _err("no products found", 404)
        cards = [m.to_card_dict() for m in matches]
        ok = send_products(wa_number, cards)
        summary = ", ".join(c.get("title", "") for c in cards)[:280]
        sc.record_admin_message(
            wa_number, _current_user(), msg_type="text",
            body=f"[product cards] {summary}", status="sent" if ok else "failed",
        )
        if ok:
            sc.mark_outbound(wa_number, f"[products] {summary}")
        sc.audit(_current_user(), "send_product_card", wa_number, summary, _client_ip())
        return jsonify({"ok": bool(ok), "sent": len(cards), "titles": [c.get("title") for c in cards]})
    except Exception as exc:  # noqa: BLE001
        logger.error("SUPPORT | send-card failed: %s", exc)
        return _err("failed to send product card", 502)


@support_bp.route("/api/shopify/draft-order/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_shopify_draft_order(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    payload = request.get_json(silent=True) or {}
    line_items = payload.get("line_items") or []
    if not isinstance(line_items, list) or not line_items:
        return _err("line_items (list of {variant_id, quantity}) is required")
    try:
        from shopify.orders import create_draft_order

        result = create_draft_order(line_items)
        if not result or not result.get("invoice_url"):
            return _err("could not create draft order", 502)
        sc.audit(_current_user(), "draft_order", wa_number,
                 result.get("draft_order_id", ""), _client_ip())
        return jsonify({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        logger.error("SUPPORT | draft order failed: %s", exc)
        return _err("draft order failed", 502)


# --------------------------------------------------------------------------- #
# Payment link
# --------------------------------------------------------------------------- #
@support_bp.route("/api/payment/link/<wa_number>", methods=["POST"])
@login_required
@csrf_protect
def api_payment_link(wa_number: str) -> Any:
    if not _valid_wa(wa_number):
        return _err("invalid number")
    payload = request.get_json(silent=True) or {}
    try:
        amount = float(payload.get("amount") or 0)
    except (TypeError, ValueError):
        return _err("valid amount is required")
    if amount <= 0:
        return _err("amount must be > 0")
    currency = (payload.get("currency") or "INR").strip()[:8]
    order_ref = (payload.get("order_number") or f"WA-{wa_number[-6:]}").strip()[:40]
    send_to_customer = bool(payload.get("send", False))

    try:
        from payments import generate_payment_link

        order = {
            "id": order_ref, "order_number": order_ref,
            "currency": currency, "total_amount": amount,
        }
        link = generate_payment_link(order)
        url = link.get("url", "")
        if not url:
            return _err("could not generate payment link", 502)

        sent = False
        if send_to_customer:
            from whatsapp.support_sender import send_text

            msg = f"Here is your secure payment link for {currency} {amount:.2f}:\n{url}"
            wamid = send_text(wa_number, msg)
            sent = bool(wamid)
            sc.record_admin_message(
                wa_number, _current_user(), msg_type="text", body=msg,
                wa_message_id=wamid, status="sent" if wamid else "failed",
            )
            if wamid:
                sc.mark_outbound(wa_number, "[payment link]")
        sc.audit(_current_user(), "payment_link", wa_number,
                 f"{currency} {amount} via {link.get('provider')}", _client_ip())
        return jsonify({"ok": True, "sent": sent, **link})
    except Exception as exc:  # noqa: BLE001
        logger.error("SUPPORT | payment link failed: %s", exc)
        return _err("payment link failed", 502)


def init_support_console(app) -> None:
    """Register the support-console blueprint on the Flask app (idempotent)."""
    if "admin_support.console" in app.view_functions:
        return
    app.register_blueprint(support_bp)
    logger.info("SUPPORT | Live support console mounted at /admin/support/console")


__all__ = ["support_bp", "init_support_console"]
