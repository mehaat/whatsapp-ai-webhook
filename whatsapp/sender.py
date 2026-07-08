"""
whatsapp/sender.py
-------------------
Outbound WhatsApp Cloud API message sending for ME-HAAT Fashion AI Bot v3.0.

Supports plain text, interactive reply buttons, interactive list messages,
and formatted product-card style messages (built on top of buttons since
Shopify-catalog-linked WhatsApp product messages require an approved
WhatsApp Commerce catalog connection, which is configured separately in
Meta Commerce Manager).
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

from config import config
from utils.logging import log_execution_time, logger

WHATSAPP_SEND_URL_TEMPLATE = (
    "https://graph.facebook.com/{version}/{phone_number_id}/messages"
)


def _send_url() -> str:
    return WHATSAPP_SEND_URL_TEMPLATE.format(
        version=config.whatsapp_api_version, phone_number_id=config.phone_number_id
    )


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {config.whatsapp_token}",
        "Content-Type": "application/json",
    }


def _post_with_retries(body: Dict) -> bool:
    """POST a message payload to the WhatsApp Cloud API with retry/backoff."""
    if not config.whatsapp_token or not config.phone_number_id:
        logger.error("WHATSAPP | Missing WHATSAPP_TOKEN or PHONE_NUMBER_ID; cannot send message")
        return False

    for attempt in range(1, config.max_retries + 1):
        try:
            response = requests.post(
                _send_url(), headers=_headers(), json=body, timeout=config.request_timeout_seconds
            )
        except requests.exceptions.Timeout:
            logger.warning("WHATSAPP | Send timed out (attempt %d/%d)", attempt, config.max_retries)
            time.sleep(min(2 ** attempt * 0.25, 4.0))
            continue
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "WHATSAPP | Send error (attempt %d/%d): %s", attempt, config.max_retries, exc
            )
            time.sleep(min(2 ** attempt * 0.25, 4.0))
            continue

        if response.status_code < 400:
            return True

        if 500 <= response.status_code < 600:
            logger.warning(
                "WHATSAPP | Server error %d (attempt %d/%d)",
                response.status_code, attempt, config.max_retries,
            )
            time.sleep(min(2 ** attempt * 0.25, 4.0))
            continue

        logger.error(
            "WHATSAPP | Send failed (%d): %s", response.status_code, response.text[:500]
        )
        return False

    logger.error("WHATSAPP | Exhausted retries sending message")
    return False


@log_execution_time
def send_text_message(to_number: str, message_text: str) -> bool:
    """Send a plain text message to a customer.

    Args:
        to_number: Recipient WhatsApp number (E.164 digits, no '+').
        message_text: Message body to send.

    Returns:
        True if the send succeeded, False otherwise.
    """
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": message_text[:4096]},
    }
    success = _post_with_retries(body)
    if success:
        logger.info("MESSAGE | Outgoing text to %s: %s", to_number, message_text[:200])
    return success


@log_execution_time
def send_button_message(
    to_number: str, body_text: str, buttons: List[Dict[str, str]]
) -> bool:
    """Send an interactive message with up to 3 quick-reply buttons.

    Args:
        to_number: Recipient WhatsApp number.
        body_text: Main message text shown above the buttons.
        buttons: List of {"id": str, "title": str} dicts (max 3, title <= 20 chars).

    Returns:
        True if the send succeeded, False otherwise.
    """
    limited_buttons = buttons[:3]
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": b["id"], "title": b["title"][:20]},
                    }
                    for b in limited_buttons
                ]
            },
        },
    }
    return _post_with_retries(body)


@log_execution_time
def send_list_message(
    to_number: str,
    body_text: str,
    button_label: str,
    sections: List[Dict],
) -> bool:
    """Send an interactive list message (e.g. for browsing product categories).

    Args:
        to_number: Recipient WhatsApp number.
        body_text: Main message text.
        button_label: Label for the button that opens the list.
        sections: List of {"title": str, "rows": [{"id", "title", "description"}]}.

    Returns:
        True if the send succeeded, False otherwise.
    """
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text[:1024]},
            "action": {
                "button": button_label[:20],
                "sections": sections,
            },
        },
    }
    return _post_with_retries(body)


def send_product_card(to_number: str, products: List[Dict[str, str]]) -> bool:
    """Send a formatted, text-based product-card style message.

    Each product dict expects keys: title, price, currency, stock_label, url.

    Args:
        to_number: Recipient WhatsApp number.
        products: List of verified product summary dicts.

    Returns:
        True if the send succeeded, False otherwise.
    """
    if not products:
        return send_text_message(
            to_number, "No matching verified products were found for your search."
        )

    lines = ["Here's what I found for you:\n"]
    for p in products:
        lines.append(
            f"🧵 *{p.get('title', 'Product')}*\n"
            f"   Price: {p.get('currency', 'INR')} {p.get('price', 'N/A')}\n"
            f"   Availability: {p.get('stock_label', 'Unknown')}"
        )
    text = "\n\n".join(lines)
    return send_text_message(to_number, text)


def send_order_status_reply(to_number: str, order_summary: str) -> bool:
    """Send a formatted order status reply.

    Args:
        to_number: Recipient WhatsApp number.
        order_summary: Verified, pre-formatted order status text.

    Returns:
        True if the send succeeded, False otherwise.
    """
    return send_text_message(to_number, f"📦 Order Update:\n{order_summary}")


def mark_message_as_read(message_id: str) -> bool:
    """Mark an inbound message as read (blue ticks) via the Cloud API.

    Args:
        message_id: The WhatsApp message ID (`wamid...`) to mark read.

    Returns:
        True if the request succeeded, False otherwise.
    """
    body = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    return _post_with_retries(body)
