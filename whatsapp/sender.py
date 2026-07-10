"""
whatsapp/sender.py
-------------------
Outbound WhatsApp Cloud API message sending for ME-HAAT Fashion AI Bot v4.0.

Supports plain text, interactive reply buttons, interactive list messages,
rich formatted product cards, and — when a Meta Commerce catalog is connected
— native WhatsApp Product Messages (with automatic fallback to formatted text
cards when no catalog is configured or the native send fails).

v4.0 additions (backward compatible):
    - ``send_product_card`` now renders Title / Price / Currency / Availability /
      Category / Variant count / Short description / Product URL (max 5 items).
    - ``send_catalog_product_message`` sends a native interactive ``product_list``.
    - ``send_products`` is a smart dispatcher: native catalog first, text second.
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

# Local currency-symbol map so this module stays independent of the Shopify layer.
_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "د.إ"}

# Maximum product cards shown in a single reply (Task 8).
MAX_PRODUCTS_PER_MESSAGE = 5


def _symbol_for(currency: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    return _CURRENCY_SYMBOLS.get((currency or "").upper(), currency or "")


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


# --------------------------------------------------------------------------
# Product cards
# --------------------------------------------------------------------------

def _format_product_card(index: int, p: Dict[str, object]) -> str:
    """Render one verified product as a nicely formatted WhatsApp text card.

    Expected keys (all optional except title): title, price, currency,
    currency_symbol, stock_label / in_stock, product_type, variant_count,
    short_description, url.
    """
    title = str(p.get("title", "Product")).strip()
    currency = str(p.get("currency", "INR"))
    symbol = _symbol_for(currency, str(p.get("currency_symbol", "")))
    price = p.get("price", "N/A")

    if "stock_label" in p and p.get("stock_label"):
        stock_label = str(p["stock_label"])
    else:
        stock_label = "In Stock" if p.get("in_stock") else "Out of Stock"
    stock_emoji = "✅" if stock_label.lower().startswith("in") else "❌"

    lines = [f"{index}. 🧵 *{title}*"]
    lines.append(f"💰 {symbol}{price}")
    lines.append(f"{stock_emoji} {stock_label}")

    product_type = str(p.get("product_type", "")).strip()
    if product_type:
        lines.append(f"📦 Category: {product_type}")

    variant_count = p.get("variant_count")
    if variant_count:
        lines.append(f"🧶 Options: {variant_count} variant(s)")

    short_desc = str(p.get("short_description", "")).strip()
    if short_desc:
        lines.append(f"📝 {short_desc}")

    url = str(p.get("url", "")).strip()
    if url:
        lines.append(f"🔗 {url}")

    return "\n".join(lines)


def _dedupe_products(products: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Remove duplicate products by id (or title) while preserving order."""
    seen = set()
    unique: List[Dict[str, object]] = []
    for p in products:
        key = p.get("product_id") or p.get("retailer_id") or str(p.get("title", "")).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def send_product_card(
    to_number: str,
    products: List[Dict[str, object]],
    header: str = "Here's what I found for you 🛍️",
) -> bool:
    """Send a formatted, text-based product-card message (max 5 products).

    Each product dict may contain: title, price, currency, currency_symbol,
    stock_label / in_stock, product_type, variant_count, short_description, url.
    Only ``title`` is strictly required; everything else degrades gracefully.

    Args:
        to_number: Recipient WhatsApp number.
        products: List of verified product summary dicts.
        header: Optional intro line shown above the cards.

    Returns:
        True if the send succeeded, False otherwise.
    """
    if not products:
        return send_text_message(
            to_number, "No matching verified products were found for your search."
        )

    unique = _dedupe_products(products)[:MAX_PRODUCTS_PER_MESSAGE]
    cards = [_format_product_card(i, p) for i, p in enumerate(unique, start=1)]
    text = header + "\n\n" + "\n\n".join(cards)
    return send_text_message(to_number, text)


@log_execution_time
def send_catalog_product_message(
    to_number: str,
    product_retailer_ids: List[str],
    body_text: str,
    header_text: str = "ME-HAAT Fashion",
    footer_text: str = "",
    catalog_id: Optional[str] = None,
) -> bool:
    """Send a native WhatsApp interactive ``product_list`` message.

    Requires a connected Meta Commerce catalog (``WHATSAPP_CATALOG_ID``). Items
    are referenced by their ``retailer_id`` (the id you assigned to the item in
    the catalog / product feed).

    Args:
        to_number: Recipient WhatsApp number.
        product_retailer_ids: Catalog item retailer ids to show.
        body_text: Body text shown above the products.
        header_text: Short header title.
        footer_text: Optional footer text.
        catalog_id: Override catalog id; defaults to ``config.whatsapp_catalog_id``.

    Returns:
        True if the native send succeeded, False otherwise.
    """
    resolved_catalog = catalog_id or config.whatsapp_catalog_id
    if not resolved_catalog or not product_retailer_ids:
        return False

    product_items = [
        {"product_retailer_id": str(rid)} for rid in product_retailer_ids[:30] if rid
    ]
    interactive: Dict[str, object] = {
        "type": "product_list",
        "header": {"type": "text", "text": header_text[:60]},
        "body": {"text": body_text[:1024]},
        "action": {
            "catalog_id": str(resolved_catalog),
            "sections": [
                {"title": "Featured", "product_items": product_items}
            ],
        },
    }
    if footer_text:
        interactive["footer"] = {"text": footer_text[:60]}

    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": interactive,
    }
    return _post_with_retries(body)


def send_products(
    to_number: str,
    products: List[Dict[str, object]],
    header: str = "Here's what I found for you 🛍️",
    body_text: str = "Tap a product to see full details and buy on WhatsApp.",
) -> bool:
    """Smart product dispatcher (Task 11).

    If a Meta Commerce catalog is connected (``WHATSAPP_CATALOG_ID``), attempt a
    native WhatsApp Product Message first; on any failure — or when no catalog
    is configured — fall back to formatted text product cards. Always caps at
    ``MAX_PRODUCTS_PER_MESSAGE`` products.

    Args:
        to_number: Recipient WhatsApp number.
        products: List of verified product card dicts.
        header: Intro line for the text-card fallback.
        body_text: Body text for the native catalog message.

    Returns:
        True if any variant of the message was delivered.
    """
    if not products:
        return send_product_card(to_number, products, header=header)

    unique = _dedupe_products(products)[:MAX_PRODUCTS_PER_MESSAGE]

    if config.whatsapp_catalog_id:
        retailer_ids = [str(p.get("retailer_id")) for p in unique if p.get("retailer_id")]
        if retailer_ids and send_catalog_product_message(
            to_number, retailer_ids, body_text=body_text, header_text="ME-HAAT Fashion"
        ):
            logger.info("MESSAGE | Sent native catalog product message to %s", to_number)
            return True
        logger.info(
            "MESSAGE | Native catalog send unavailable/failed for %s; using text cards", to_number
        )

    return send_product_card(to_number, unique, header=header)


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
