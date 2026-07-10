"""
admin/shopify_lookup.py
------------------------
Thin adapter over the existing ``shopify.orders`` module so the dashboard's
Order Lookup page can query **live, verified** Shopify data without duplicating
any Shopify logic. Results are normalised to plain dicts and every call is
guarded so a missing/unconnected store yields a friendly "not connected"
payload instead of an exception.

Store connectivity is derived from the existing token store and the configured
default shop — nothing here changes the OAuth flow or the token store.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import config
from utils.logging import logger

try:
    from shopify.auth import token_store
except Exception as exc:  # noqa: BLE001 - never let the dashboard fail to import
    token_store = None  # type: ignore
    logger.debug("ADMIN | token_store unavailable to dashboard: %s", exc)


def store_connected() -> bool:
    """True when at least one Shopify store is installed or a default is set."""
    try:
        if token_store is not None and token_store.list_shops():
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADMIN | store_connected check failed: %s", exc)
    return bool(config.default_shop_domain)


def default_shop() -> Optional[str]:
    """Return the shop domain used to build 'open in Shopify' admin links."""
    try:
        if token_store is not None:
            shop = token_store.get_default_shop()
            if shop:
                return shop
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADMIN | default_shop lookup failed: %s", exc)
    return config.default_shop_domain or None


def _order_to_dict(order: Any) -> Dict[str, Any]:
    """Normalise an ``OrderStatus`` dataclass into a JSON-friendly dict."""
    tracking = ", ".join(getattr(order, "tracking_numbers", []) or []) or ""
    tracking_url = ""
    urls = getattr(order, "tracking_urls", []) or []
    if urls:
        tracking_url = urls[0]
    admin_url = ""
    shop = default_shop()
    order_id = getattr(order, "order_id", None)
    if shop and order_id:
        admin_url = f"https://{shop}/admin/orders/{order_id}"
    return {
        "order_id": order_id,
        "order_name": getattr(order, "name", ""),
        "financial_status": getattr(order, "financial_status", ""),
        "fulfillment_status": getattr(order, "fulfillment_status", "") or "unfulfilled",
        "total_price": getattr(order, "total_price", ""),
        "currency": getattr(order, "currency", ""),
        "tracking": tracking,
        "tracking_url": tracking_url,
        "admin_url": admin_url,
    }


def lookup(query: str, by: str = "auto") -> Dict[str, Any]:
    """Look up orders by order number / phone (and customer by phone).

    Args:
        query: The search term (order name/number, or phone).
        by: 'order', 'phone', or 'auto' (infer from the query shape).

    Returns:
        A dict with ``connected`` flag, an ``orders`` list and optional
        ``customer`` block. Never raises.
    """
    query = (query or "").strip()
    if not query:
        return {"connected": store_connected(), "orders": [], "customer": None, "query": query}
    if not store_connected():
        return {
            "connected": False,
            "orders": [],
            "customer": None,
            "query": query,
            "message": "No Shopify store is connected. Install via /shopify/install.",
        }

    from shopify import orders as shopify_orders

    orders: List[Dict[str, Any]] = []
    customer: Optional[Dict[str, Any]] = None

    looks_like_phone = sum(ch.isdigit() for ch in query) >= 7 and "#" not in query
    mode = by if by in {"order", "phone"} else ("phone" if looks_like_phone else "order")

    try:
        if mode == "order":
            found = shopify_orders.find_order_by_name(query)
            if found is not None:
                orders.append(_order_to_dict(found))
        else:  # phone
            for order in shopify_orders.find_orders_by_phone(query, limit=10):
                orders.append(_order_to_dict(order))
            record = shopify_orders.find_customer_by_phone(query)
            if record is not None:
                customer = {
                    "customer_id": getattr(record, "customer_id", None),
                    "first_name": getattr(record, "first_name", ""),
                    "last_name": getattr(record, "last_name", ""),
                    "email": getattr(record, "email", ""),
                    "phone": getattr(record, "phone", ""),
                    "orders_count": getattr(record, "orders_count", 0),
                }
    except Exception as exc:  # noqa: BLE001 - surface as empty result, never 500
        logger.error("ADMIN | Shopify lookup failed for %r: %s", query, exc)
        return {
            "connected": True,
            "orders": [],
            "customer": None,
            "query": query,
            "message": "Shopify lookup failed. Check store connection and API scopes.",
        }

    return {"connected": True, "orders": orders, "customer": customer, "query": query}
