"""
commerce/webhook_orders.py
---------------------------
Ingestion of WhatsApp Commerce (catalog) orders — the ``message.type == "order"``
path. Orchestrates the full v6.0 receive flow:

    parse -> stock validation -> persist order -> Shopify draft order ->
    "order received" customer notification -> admin alert.

Every step is guarded: a failure in any stage is logged and the flow continues
so a paying customer always at least gets their order recorded and acknowledged.
This function runs inside the webhook request (single-worker deployment), so it
is written to be quick and never raise.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from config import config
from utils.logging import logger


def handle_catalog_order(message: Dict[str, Any], profile_name: str = "") -> Optional[Dict[str, Any]]:
    """Process a Meta ``type == "order"`` webhook message end to end.

    Returns the created order dict (or None when nothing was created, e.g. an
    empty order or an out-of-stock rejection). Never raises.
    """
    from commerce.schema import ParsedOrder
    from commerce.service import order_service

    try:
        parsed = ParsedOrder.from_whatsapp(
            message, profile_name=profile_name, default_currency=config.default_currency
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | Failed to parse catalog order: %s", exc)
        return None

    if not parsed.items:
        logger.info("COMMERCE | Catalog order from %s had no items; ignoring", parsed.wa_number)
        return None

    # 1) Stock validation (opt-in; fails open).
    try:
        from commerce.stock import validate_stock

        stock = validate_stock(parsed.items)
        if not stock.ok:
            _reply_out_of_stock(parsed.wa_number, stock.unavailable, parsed.language)
            logger.info("COMMERCE | Order from %s rejected (out of stock): %s",
                        parsed.wa_number, stock.unavailable)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("COMMERCE | Stock validation error (allowing order): %s", exc)

    # 2) Persist the order.
    try:
        order = order_service.create_order(parsed)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | Failed to persist order for %s: %s", parsed.wa_number, exc)
        return None

    # 3) Auto-create a Shopify draft order (best-effort).
    if config.auto_draft_order:
        try:
            from commerce.draft_orders import create_draft_for_order

            create_draft_for_order(order)
            # Refresh so downstream steps see any checkout/invoice URL.
            refreshed = order_service.get_order(order_id=order["id"], include_items=True)
            if refreshed:
                order = refreshed
        except Exception as exc:  # noqa: BLE001
            logger.error("COMMERCE | Draft order step failed for %s: %s",
                         order.get("order_number"), exc)

    # 4) Notify the customer that the order was received.
    try:
        from commerce.notifications import notify_order_received

        notify_order_received(order)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE | order-received notification failed: %s", exc)

    # 5) Alert the admin (if configured).
    _notify_admin_new_order(order)

    logger.info("COMMERCE | Catalog order %s ingested for %s",
                order.get("order_number"), parsed.wa_number)
    return order


def _reply_out_of_stock(wa_number: str, unavailable, language: Optional[str]) -> None:
    """Send the out-of-stock reply required by the spec."""
    try:
        from whatsapp.sender import send_text_message

        names = ", ".join([u for u in unavailable if u]) if unavailable else ""
        if (language or "").lower() == "hindi":
            text = "माफ़ कीजिए.\n\nयह प्रोडक्ट अभी स्टॉक में नहीं है."
        else:
            text = "Sorry.\n\nThis product is currently out of stock."
        if names:
            text += f"\n\n({names})"
        send_text_message(wa_number, text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("COMMERCE | out-of-stock reply failed: %s", exc)


def _notify_admin_new_order(order: Dict[str, Any]) -> None:
    """Send a new-order alert to the admin WhatsApp number, if configured."""
    admin_number = config.admin_whatsapp_number
    if not admin_number:
        return
    try:
        from commerce.notifications import notify_admin

        text = (
            f"🛒 New order {order.get('order_number')}\n"
            f"Customer: {order.get('customer_name') or order.get('wa_number')}\n"
            f"Amount: {order.get('currency')} {order.get('total_amount')}\n"
            f"Items: {len(order.get('items', []))}"
        )
        notify_admin(text, admin_number)
    except Exception as exc:  # noqa: BLE001
        logger.debug("COMMERCE | admin alert failed: %s", exc)
