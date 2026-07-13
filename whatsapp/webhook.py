"""
whatsapp/webhook.py
--------------------
WhatsApp Cloud API webhook for ME-HAAT Fashion AI Bot v5.1 Production Edition.

Handles the GET verification handshake and POST message/status events.
Actual business-logic handling of an incoming message is delegated to a
callback registered via ``init_webhook`` — this keeps the webhook module
free of circular imports with the orchestration layer in ``app.py``.

v5.1 hardening (backward compatible):
    - Inbound POSTs are verified against Meta's ``X-Hub-Signature-256`` header
      when ``WHATSAPP_APP_SECRET`` is configured (forged calls are rejected).
    - Inbound message IDs are de-duplicated so Meta webhook retries never cause
      the bot to reply to the same message twice.
    - Inbound messages are best-effort marked as read (blue ticks).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from threading import Lock
from typing import Any, Callable, Dict, Optional

from flask import Blueprint, jsonify, request

from config import config
from utils.logging import logger
from utils.ratelimit import RateLimiter

whatsapp_webhook_bp = Blueprint("whatsapp_webhook", __name__)

# Type: (wa_number: str, text: str, profile_name: str) -> None
MessageHandler = Callable[[str, str, str], None]
# Type: (message: dict, profile_name: str) -> None  (WhatsApp catalog orders)
OrderHandler = Callable[[Dict[str, Any], str], None]

_message_handler: Optional[MessageHandler] = None
_order_handler: Optional[OrderHandler] = None
_rate_limiter = RateLimiter(max_requests=20, window_seconds=60)

# --------------------------------------------------------------------------
# Inbound message de-duplication
# --------------------------------------------------------------------------
# Meta re-delivers webhooks until it receives a 200, so the same inbound
# message id can arrive several times. We remember a bounded window of recently
# processed ids and skip duplicates. In-process only (see the module docstrings
# elsewhere about single-worker deployment); good enough to stop the common
# double-reply case within a worker.
_DEDUPE_CAPACITY = 2048
_seen_message_ids: "OrderedDict[str, None]" = OrderedDict()
_dedupe_lock = Lock()

# Warn only once when the app secret is missing, to avoid log spam per request.
_warned_missing_secret = False


def init_webhook(handler: MessageHandler) -> None:
    """Register the orchestration callback invoked for each inbound text message.

    Args:
        handler: Callable taking (wa_number, message_text, profile_name).
    """
    global _message_handler
    _message_handler = handler


def init_order_webhook(handler: OrderHandler) -> None:
    """Register the callback invoked for each inbound WhatsApp catalog order.

    Args:
        handler: Callable taking (message: dict, profile_name: str). Optional;
            when unset, order messages fall back to the normal text pipeline.
    """
    global _order_handler
    _order_handler = handler


def _already_processed(message_id: str) -> bool:
    """Return True if ``message_id`` was seen recently; otherwise record it."""
    if not message_id:
        return False
    with _dedupe_lock:
        if message_id in _seen_message_ids:
            # Refresh recency so it stays in the window a little longer.
            _seen_message_ids.move_to_end(message_id)
            return True
        _seen_message_ids[message_id] = None
        while len(_seen_message_ids) > _DEDUPE_CAPACITY:
            _seen_message_ids.popitem(last=False)
        return False


def _verify_signature(raw_body: bytes) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` header against the app secret.

    Returns True when the request is authentic OR when verification is disabled
    (no ``WHATSAPP_APP_SECRET`` configured). Returns False only when a secret is
    configured and the signature is missing or does not match.
    """
    global _warned_missing_secret
    secret = config.whatsapp_app_secret
    if not secret:
        if not _warned_missing_secret:
            logger.warning(
                "WEBHOOK | WHATSAPP_APP_SECRET not set — inbound webhook "
                "signatures are NOT verified. Set it for production."
            )
            _warned_missing_secret = True
        return True

    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        logger.warning("WEBHOOK | Missing/invalid X-Hub-Signature-256 header")
        return False

    provided = header.split("=", 1)[1].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        logger.warning("WEBHOOK | X-Hub-Signature-256 mismatch — rejecting payload")
        return False
    return True


@whatsapp_webhook_bp.route("/webhook", methods=["GET"])
def verify_webhook() -> Any:
    """Handle Meta's webhook verification handshake."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == config.verify_token and config.verify_token:
        logger.info("WEBHOOK | Verification succeeded")
        return challenge, 200

    logger.warning("WEBHOOK | Verification failed (mode=%s)", mode)
    return jsonify({"error": "Verification failed"}), 403


@whatsapp_webhook_bp.route("/webhook", methods=["POST"])
def handle_webhook() -> Any:
    """Handle incoming WhatsApp messages and status updates."""
    raw_body = request.get_data() or b""

    # Reject forged/unsigned payloads when an app secret is configured.
    if not _verify_signature(raw_body):
        return jsonify({"status": "forbidden"}), 403

    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                _resolve_tenant(value)
                _process_status_updates(value)
                _process_messages(value)
    except Exception as exc:  # noqa: BLE001 - boundary catch-all, never crash the webhook
        logger.error("WEBHOOK | Unexpected error while processing payload: %s", exc)
        return jsonify({"status": "error"}), 200

    return jsonify({"status": "received"}), 200


def _resolve_tenant(value: Dict[str, Any]) -> None:
    """Resolve the tenant for this webhook batch from its phone_number_id (v8.0).

    Sets the request-scoped tenant so orders created downstream are tagged. Fully
    guarded — when multi-tenant is off this resolves to the default tenant.
    """
    try:
        from commerce.tenancy import resolve_from_wa_webhook

        resolve_from_wa_webhook(value)
    except Exception as exc:  # noqa: BLE001 - tenancy is optional
        logger.debug("WEBHOOK | tenant resolution skipped: %s", exc)


def _process_status_updates(value: Dict[str, Any]) -> None:
    """Log message status updates (sent, delivered, read, failed)."""
    for status in value.get("statuses", []):
        status_type = status.get("status")
        recipient = status.get("recipient_id")
        logger.info("STATUS | %s -> %s", recipient, status_type)
        if status_type == "failed":
            logger.error("STATUS | Message failed for %s: %s", recipient, status.get("errors", []))


def _mark_read_best_effort(message_id: str) -> None:
    """Mark an inbound message as read (blue ticks); never raise."""
    if not message_id:
        return
    try:
        from whatsapp.sender import mark_message_as_read

        mark_message_as_read(message_id)
    except Exception as exc:  # noqa: BLE001 - cosmetic, must never break the webhook
        logger.debug("WEBHOOK | mark_message_as_read failed: %s", exc)


def _process_messages(value: Dict[str, Any]) -> None:
    """Process each inbound customer message found in the webhook payload."""
    contacts = value.get("contacts", [])
    profile_name = ""
    if contacts:
        profile_name = contacts[0].get("profile", {}).get("name", "")

    for message in value.get("messages", []):
        wa_number = message.get("from", "")
        if not wa_number:
            continue

        message_id = message.get("id", "")
        if _already_processed(message_id):
            logger.info("MESSAGE | Duplicate webhook for %s (id=%s); skipping", wa_number, message_id)
            continue

        # Best-effort read receipt; independent of downstream handling.
        _mark_read_best_effort(message_id)

        message_type = message.get("type")

        # WhatsApp Commerce catalog order (v6.0): route to the order handler
        # before the text pipeline, since order messages carry no text body.
        if message_type == "order" and _order_handler is not None:
            logger.info("ORDER | Incoming catalog order from %s", wa_number)
            try:
                _order_handler(message, profile_name)
            except Exception as exc:  # noqa: BLE001 - never crash the webhook
                logger.error("ORDER | Order handler failed for %s: %s", wa_number, exc)
            continue

        if not _rate_limiter.is_allowed(wa_number):
            logger.warning("RATE_LIMIT | Blocked message from %s", wa_number)
            _dispatch(wa_number, "__RATE_LIMITED__", profile_name)
            continue

        text = _extract_text_from_message(message, message_type)

        if text is None:
            logger.info("MESSAGE | Unsupported message type '%s' from %s", message_type, wa_number)
            _dispatch(wa_number, "__UNSUPPORTED_TYPE__", profile_name)
            continue

        logger.info("MESSAGE | Incoming from %s: %s", wa_number, text)
        _dispatch(wa_number, text, profile_name)


def _extract_text_from_message(message: Dict[str, Any], message_type: str) -> Optional[str]:
    """Extract a normalized text string from any supported inbound message type.

    Handles plain text, interactive button replies, and interactive list replies.
    """
    if message_type == "text":
        return message.get("text", {}).get("body", "")

    if message_type == "interactive":
        interactive = message.get("interactive", {})
        interactive_type = interactive.get("type")
        if interactive_type == "button_reply":
            return interactive.get("button_reply", {}).get("title", "")
        if interactive_type == "list_reply":
            return interactive.get("list_reply", {}).get("title", "")

    return None


def _dispatch(wa_number: str, text: str, profile_name: str) -> None:
    """Invoke the registered message handler, guarding against misconfiguration."""
    if _message_handler is None:
        logger.error("WEBHOOK | No message handler registered; dropping message from %s", wa_number)
        return
    _message_handler(wa_number, text, profile_name)
