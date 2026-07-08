"""
whatsapp/webhook.py
--------------------
WhatsApp Cloud API webhook for ME-HAAT Fashion AI Bot v3.0.

Handles the GET verification handshake and POST message/status events.
Actual business-logic handling of an incoming message is delegated to a
callback registered via ``init_webhook`` — this keeps the webhook module
free of circular imports with the orchestration layer in ``app.py``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from flask import Blueprint, jsonify, request

from config import config
from utils.logging import logger
from utils.ratelimit import RateLimiter

whatsapp_webhook_bp = Blueprint("whatsapp_webhook", __name__)

# Type: (wa_number: str, text: str, profile_name: str) -> None
MessageHandler = Callable[[str, str, str], None]

_message_handler: Optional[MessageHandler] = None
_rate_limiter = RateLimiter(max_requests=20, window_seconds=60)


def init_webhook(handler: MessageHandler) -> None:
    """Register the orchestration callback invoked for each inbound text message.

    Args:
        handler: Callable taking (wa_number, message_text, profile_name).
    """
    global _message_handler
    _message_handler = handler


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
    payload = request.get_json(force=True, silent=True) or {}

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                _process_status_updates(value)
                _process_messages(value)
    except Exception as exc:  # noqa: BLE001 - boundary catch-all, never crash the webhook
        logger.error("WEBHOOK | Unexpected error while processing payload: %s", exc)
        return jsonify({"status": "error"}), 200

    return jsonify({"status": "received"}), 200


def _process_status_updates(value: Dict[str, Any]) -> None:
    """Log message status updates (sent, delivered, read, failed)."""
    for status in value.get("statuses", []):
        status_type = status.get("status")
        recipient = status.get("recipient_id")
        logger.info("STATUS | %s -> %s", recipient, status_type)
        if status_type == "failed":
            logger.error("STATUS | Message failed for %s: %s", recipient, status.get("errors", []))


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

        if not _rate_limiter.is_allowed(wa_number):
            logger.warning("RATE_LIMIT | Blocked message from %s", wa_number)
            _dispatch(wa_number, "__RATE_LIMITED__", profile_name)
            continue

        message_type = message.get("type")
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
