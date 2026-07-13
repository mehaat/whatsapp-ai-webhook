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

    # 3) Offload side effects to the background queue so the webhook returns
    #    fast. When JOBS_ENABLED is false, run_async executes each handler
    #    synchronously, giving identical behaviour without workers.
    try:
        from commerce.jobs import run_async

        if config.inventory_reservation_enabled:
            run_async("reserve", {"order_id": order["id"]})
        if config.auto_draft_order:
            run_async("draft_order", {"order_id": order["id"]})
        run_async("notify", {"order_id": order["id"], "status": "received"})
    except Exception as exc:  # noqa: BLE001 - side effects must never fail ingestion
        logger.error("COMMERCE | Failed to enqueue order side effects for %s: %s",
                     order.get("order_number"), exc)
        # Last-resort synchronous fallback for the customer acknowledgement.
        try:
            from commerce.notifications import notify_order_received

            notify_order_received(order)
        except Exception:  # noqa: BLE001
            pass

    # 4) Alert the admin (if configured) — quick, best-effort.
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
