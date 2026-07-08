"""
shopify/inventory.py
---------------------
Verified inventory / stock-level checks for ME-HAAT Fashion AI Bot v3.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from shopify.client import get_client_for_shop
from utils.logging import logger


@dataclass
class InventoryStatus:
    """Verified inventory status for a single product variant."""

    variant_id: int
    product_title: str
    variant_title: str
    available: bool
    quantity: int


def check_variant_inventory(
    variant_id: int, shop: Optional[str] = None
) -> Optional[InventoryStatus]:
    """Check live inventory for a specific product variant.

    Args:
        variant_id: Shopify variant ID.
        shop: Shop domain (defaults to configured default shop).

    Returns:
        An ``InventoryStatus`` object, or None if not found / shop not connected.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return None

    variant_response = client.get(f"variants/{variant_id}.json")
    if not variant_response or "variant" not in variant_response:
        return None

    variant = variant_response["variant"]
    product_id = variant.get("product_id")

    product_title = ""
    if product_id:
        product_response = client.get(f"products/{product_id}.json", params={"fields": "title"})
        if product_response and "product" in product_response:
            product_title = product_response["product"].get("title", "")

    quantity = variant.get("inventory_quantity") or 0
    available = bool(variant.get("available", quantity > 0))

    return InventoryStatus(
        variant_id=variant.get("id", variant_id),
        product_title=product_title,
        variant_title=variant.get("title", "Default"),
        available=available,
        quantity=quantity,
    )


def check_product_availability(product_id: int, shop: Optional[str] = None) -> List[InventoryStatus]:
    """Check live inventory for every variant of a product.

    Args:
        product_id: Shopify product ID.
        shop: Shop domain (defaults to configured default shop).

    Returns:
        A list of ``InventoryStatus`` objects, one per variant.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return []

    response = client.get(f"products/{product_id}.json")
    if not response or "product" not in response:
        return []

    product = response["product"]
    product_title = product.get("title", "")
    statuses: List[InventoryStatus] = []

    for variant in product.get("variants", []):
        quantity = variant.get("inventory_quantity") or 0
        available = bool(variant.get("available", quantity > 0))
        statuses.append(
            InventoryStatus(
                variant_id=variant.get("id", 0),
                product_title=product_title,
                variant_title=variant.get("title", "Default"),
                available=available,
                quantity=quantity,
            )
        )

    return statuses
