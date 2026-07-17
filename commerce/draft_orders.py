"""
commerce/draft_orders.py
-------------------------
Automatic Shopify Draft Order creation for v6.0 commerce orders.

When a WhatsApp catalog order is received we create a matching Shopify draft
order so the merchant gets a real, invoiceable order in their Shopify admin and
the customer can be given a hosted checkout/invoice URL. This is best-effort:
if Shopify is not connected, no line item maps to a variant, or the API call
fails, we log and continue — the order still exists in our own store and the
admin can generate a payment link manually.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from utils.logging import logger

from commerce.stock import resolve_variant_id


def _line_items_from_order(order: Dict[str, Any]) -> List[Dict[str, int]]:
    """Map order items to Shopify draft-order line items ({variant_id, quantity})."""
    line_items: List[Dict[str, int]] = []
    for item in order.get("items", []):
        variant_id = resolve_variant_id(item)
        if variant_id is None:
            continue
        line_items.append({"variant_id": variant_id, "quantity": int(item.get("quantity", 1) or 1)})
    return line_items


def create_draft_for_order(order: Dict[str, Any], shop: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Create a Shopify draft order for ``order`` and persist the links.

    Returns the ``{draft_order_id, invoice_url}`` dict from Shopify on success,
    or ``None`` when the draft could not be created (never raises).
    """
    try:
        from shopify.orders import create_draft_order
    except Exception as exc:  # noqa: BLE001
        logger.warning("DRAFT_ORDER | Shopify orders module unavailable: %s", exc)
        return None

    line_items = _line_items_from_order(order)
    if not line_items:
        logger.info(
            "DRAFT_ORDER | Order %s has no variant-resolvable items; skipping Shopify draft",
            order.get("order_number"),
        )
        return None

    try:
        result = create_draft_order(line_items, shop=shop)
    except Exception as exc:  # noqa: BLE001
        logger.error("DRAFT_ORDER | create_draft_order failed for %s: %s",
                     order.get("order_number"), exc)
        return None

    if not result:
        logger.info("DRAFT_ORDER | Shopify draft not created for %s (shop not connected?)",
                    order.get("order_number"))
        return None

    # Persist the linkage back onto our order.
    try:
        from commerce.service import order_service

        order_service.set_shopify_links(
            order["id"],
            draft_order_id=result.get("draft_order_id"),
            invoice_url=result.get("invoice_url"),
            checkout_url=result.get("invoice_url"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("DRAFT_ORDER | Failed to persist Shopify links: %s", exc)

    logger.info("DRAFT_ORDER | Created Shopify draft %s for order %s",
                result.get("draft_order_id"), order.get("order_number"))
    return result
