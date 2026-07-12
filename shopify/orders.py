"""
shopify/orders.py
------------------
Order-related verified data for ME-HAAT Fashion AI Bot v3.0:
    - Order status lookup (by order name / number)
    - Draft order creation (for assisted/manual checkout flows)
    - Checkout link and cart permalink generation
    - Customer lookup (by phone/email)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from shopify.client import get_client_for_shop
from utils.logging import logger


@dataclass
class OrderStatus:
    """Verified order status information."""

    order_id: int
    name: str
    financial_status: str
    fulfillment_status: str
    total_price: str
    currency: str
    tracking_numbers: List[str] = field(default_factory=list)
    tracking_urls: List[str] = field(default_factory=list)

    def to_context_line(self) -> str:
        """Render as a verified-context line for Gemini."""
        tracking = ", ".join(self.tracking_numbers) if self.tracking_numbers else "Not yet available"
        return (
            f"Order {self.name} | Payment: {self.financial_status} | "
            f"Fulfillment: {self.fulfillment_status or 'unfulfilled'} | "
            f"Total: {self.currency} {self.total_price} | Tracking: {tracking}"
        )


@dataclass
class CustomerRecord:
    """Verified customer record."""

    customer_id: int
    first_name: str
    last_name: str
    email: str
    phone: str
    orders_count: int


def find_order_by_name(order_name: str, shop: Optional[str] = None) -> Optional[OrderStatus]:
    """Look up verified order status by order name/number (e.g. "#1001").

    Args:
        order_name: The order name/number as given by the customer.
        shop: Shop domain (defaults to configured default shop).

    Returns:
        An ``OrderStatus`` object, or None if not found / shop not connected.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return None

    normalized_name = order_name if order_name.startswith("#") else f"#{order_name}"

    response = client.get(
        "orders.json", params={"name": normalized_name, "status": "any"}
    )
    if not response:
        return None

    orders = response.get("orders", [])
    if not orders:
        return None

    return _normalize_order(orders[0])


def find_orders_by_phone(phone: str, shop: Optional[str] = None, limit: int = 5) -> List[OrderStatus]:
    """Look up recent verified orders for a customer by phone number.

    Args:
        phone: Customer's phone number.
        shop: Shop domain (defaults to configured default shop).
        limit: Maximum number of orders to return.

    Returns:
        A list of ``OrderStatus`` objects, most recent first.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return []

    customers_response = client.get("customers/search.json", params={"query": f"phone:{phone}"})
    if not customers_response:
        return []

    customers = customers_response.get("customers", [])
    if not customers:
        return []

    customer_id = customers[0].get("id")
    orders_response = client.get(
        "orders.json",
        params={"customer_id": customer_id, "status": "any", "limit": str(limit)},
    )
    if not orders_response:
        return []

    return [_normalize_order(o) for o in orders_response.get("orders", [])]


def _normalize_order(raw: Dict) -> OrderStatus:
    """Convert a raw Shopify order dict into a normalized ``OrderStatus``."""
    fulfillments = raw.get("fulfillments", [])
    tracking_numbers = []
    tracking_urls = []
    for f in fulfillments:
        tracking_numbers.extend(f.get("tracking_numbers", []) or [])
        tracking_urls.extend(f.get("tracking_urls", []) or [])

    return OrderStatus(
        order_id=raw.get("id", 0),
        name=raw.get("name", ""),
        financial_status=raw.get("financial_status", "unknown"),
        fulfillment_status=raw.get("fulfillment_status") or "unfulfilled",
        total_price=raw.get("total_price", "0.00"),
        currency=raw.get("currency", "INR"),
        tracking_numbers=tracking_numbers,
        tracking_urls=tracking_urls,
    )


def find_customer_by_phone(phone: str, shop: Optional[str] = None) -> Optional[CustomerRecord]:
    """Look up a verified customer record by phone number.

    Args:
        phone: Customer's phone number.
        shop: Shop domain (defaults to configured default shop).

    Returns:
        A ``CustomerRecord``, or None if not found / shop not connected.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return None

    response = client.get("customers/search.json", params={"query": f"phone:{phone}"})
    if not response:
        return None

    customers = response.get("customers", [])
    if not customers:
        return None

    c = customers[0]
    return CustomerRecord(
        customer_id=c.get("id", 0),
        first_name=c.get("first_name", "") or "",
        last_name=c.get("last_name", "") or "",
        email=c.get("email", "") or "",
        phone=c.get("phone", "") or "",
        orders_count=c.get("orders_count", 0),
    )


def create_draft_order(
    line_items: List[Dict[str, int]], shop: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """Create a Shopify draft order to generate an assisted checkout link.

    Args:
        line_items: List of {"variant_id": int, "quantity": int} dicts.
        shop: Shop domain (defaults to configured default shop).

    Returns:
        Dict with "invoice_url" and "draft_order_id" if successful, else None.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return None

    if not line_items:
        logger.warning("SHOPIFY_ORDERS | create_draft_order called with no line items")
        return None

    normalized_items = [
        {"variant_id": item["variant_id"], "quantity": item.get("quantity", 1)}
        for item in line_items
        if item.get("variant_id")
    ]
    if not normalized_items:
        logger.warning("SHOPIFY_ORDERS | create_draft_order: no line items with a variant_id")
        return None

    payload = {"draft_order": {"line_items": normalized_items}}

    response = client.post("draft_orders.json", json_body=payload)
    if not response or "draft_order" not in response:
        return None

    draft = response["draft_order"]
    return {
        "draft_order_id": str(draft.get("id", "")),
        "invoice_url": draft.get("invoice_url", ""),
    }


def build_cart_permalink(shop_domain: str, variant_quantities: Dict[int, int]) -> str:
    """Build a Shopify cart permalink for a fast, verified add-to-cart link.

    Args:
        shop_domain: The store's public domain (not myshopify.com necessarily).
        variant_quantities: Mapping of variant_id -> quantity.

    Returns:
        A cart permalink URL, e.g. "https://store.com/cart/123:1,456:2".
    """
    if not variant_quantities:
        return f"https://{shop_domain}/cart"

    parts = ",".join(f"{variant_id}:{qty}" for variant_id, qty in variant_quantities.items())
    return f"https://{shop_domain}/cart/{parts}"
