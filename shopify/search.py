"""
shopify/search.py
------------------
Verified product search and catalog lookups for ME-HAAT Fashion AI Bot v3.0.

All product facts (price, stock, variants, collections) are sourced live
from the Shopify Admin API via ``ShopifyClient`` — the AI layer must never
invent this data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from shopify.client import ShopifyClient, get_client_for_shop
from utils.logging import logger


@dataclass
class VariantMatch:
    """A single verified product variant."""

    variant_id: int
    title: str
    price: str
    available: bool
    inventory_quantity: int


@dataclass
class ProductMatch:
    """A verified, normalized product record returned to the AI layer."""

    product_id: int
    title: str
    price: str
    currency: str
    in_stock: bool
    product_type: str
    tags: List[str] = field(default_factory=list)
    url: str = ""
    variants: List[VariantMatch] = field(default_factory=list)

    def to_context_line(self) -> str:
        """Render this product as a single verified-context line for Gemini."""
        stock_label = "In Stock" if self.in_stock else "Out of Stock"
        variant_count = len(self.variants)
        return (
            f"- [ID:{self.product_id}] {self.title} | Price: {self.currency} {self.price} | "
            f"{stock_label} | Category: {self.product_type} | Variants: {variant_count}"
        )


def _shop_domain(client: ShopifyClient) -> str:
    return client.shop.replace(".myshopify.com", "")


def _normalize_product(raw: Dict, client: ShopifyClient) -> Optional[ProductMatch]:
    """Convert a raw Shopify product dict into a normalized ``ProductMatch``."""
    variants_raw = raw.get("variants", [])
    if not variants_raw:
        return None

    variants: List[VariantMatch] = []
    for v in variants_raw:
        try:
            price = float(v.get("price", "0"))
        except (TypeError, ValueError):
            continue
        inventory_qty = v.get("inventory_quantity") or 0
        available = bool(v.get("available", inventory_qty > 0))
        variants.append(
            VariantMatch(
                variant_id=v.get("id", 0),
                title=v.get("title", "Default"),
                price=f"{price:.2f}",
                available=available,
                inventory_quantity=inventory_qty,
            )
        )

    if not variants:
        return None

    lead_variant = variants[0]
    tags = [t.strip().lower() for t in raw.get("tags", "").split(",") if t.strip()]
    handle = raw.get("handle", "")
    url = f"https://{_shop_domain(client)}/products/{handle}" if handle else ""

    return ProductMatch(
        product_id=raw.get("id", 0),
        title=raw.get("title", "Unknown Product"),
        price=lead_variant.price,
        currency="INR",
        in_stock=any(v.available for v in variants),
        product_type=raw.get("product_type", "Ethnic Wear"),
        tags=tags,
        url=url,
        variants=variants,
    )


def search_products(
    shop: Optional[str] = None,
    query_text: str = "",
    max_budget: Optional[float] = None,
    min_budget: Optional[float] = None,
    color: Optional[str] = None,
    fabric: Optional[str] = None,
    occasion: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
    only_available: bool = False,
    limit: int = 5,
) -> List[ProductMatch]:
    """Search Shopify products using a combination of smart filters.

    Args:
        shop: Shop domain to query (defaults to the configured default shop).
        query_text: Free-text search term (matched against title).
        max_budget: Maximum price in store currency.
        min_budget: Minimum price in store currency.
        color: Desired color, matched against tags/title.
        fabric: Desired fabric (e.g. "silk", "cotton"), matched against tags/title.
        occasion: Desired occasion (e.g. "wedding", "festive"), matched against tags.
        category: Product type / category filter (server-side).
        tags: Additional required tags (all must match).
        only_available: If True, only return products with stock available.
        limit: Maximum number of results to return.

    Returns:
        A list of verified ``ProductMatch`` objects. Empty list if none found
        or if the shop is not connected via OAuth.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return []

    params: Dict[str, str] = {"limit": str(min(limit * 4, 100)), "status": "active"}
    if query_text:
        params["title"] = query_text
    if category:
        params["product_type"] = category

    response = client.get("products.json", params=params)
    if not response:
        return []

    raw_products = response.get("products", [])
    results: List[ProductMatch] = []

    for raw in raw_products:
        product = _normalize_product(raw, client)
        if product is None:
            continue

        if max_budget is not None and float(product.price) > max_budget:
            continue
        if min_budget is not None and float(product.price) < min_budget:
            continue

        searchable_text = f"{product.title.lower()} {' '.join(product.tags)}"
        if color and color.lower() not in searchable_text:
            continue
        if fabric and fabric.lower() not in searchable_text:
            continue
        if occasion and occasion.lower() not in searchable_text:
            continue
        if tags and not all(t.lower() in product.tags for t in tags):
            continue
        if only_available and not product.in_stock:
            continue

        results.append(product)
        if len(results) >= limit:
            break

    return results


def get_product_details(product_id: int, shop: Optional[str] = None) -> Optional[ProductMatch]:
    """Fetch full verified details for a single product by ID.

    Args:
        product_id: Shopify product ID.
        shop: Shop domain (defaults to configured default shop).

    Returns:
        A ``ProductMatch`` or None if not found / shop not connected.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return None

    response = client.get(f"products/{product_id}.json")
    if not response or "product" not in response:
        return None

    return _normalize_product(response["product"], client)


def list_collections(shop: Optional[str] = None, limit: int = 20) -> List[Dict[str, str]]:
    """List available custom + smart collections for the store.

    Args:
        shop: Shop domain (defaults to configured default shop).
        limit: Maximum collections to return per collection type.

    Returns:
        A list of {"id", "title", "handle"} dicts.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return []

    collections: List[Dict[str, str]] = []

    for endpoint in ("custom_collections.json", "smart_collections.json"):
        response = client.get(endpoint, params={"limit": str(limit)})
        if not response:
            continue
        key = "custom_collections" if "custom" in endpoint else "smart_collections"
        for item in response.get(key, []):
            collections.append(
                {
                    "id": str(item.get("id", "")),
                    "title": item.get("title", ""),
                    "handle": item.get("handle", ""),
                }
            )

    return collections


def get_products_in_collection(
    collection_id: str, shop: Optional[str] = None, limit: int = 10
) -> List[ProductMatch]:
    """List verified products belonging to a specific collection.

    Args:
        collection_id: Shopify collection ID.
        shop: Shop domain (defaults to configured default shop).
        limit: Maximum number of products to return.

    Returns:
        A list of ``ProductMatch`` objects.
    """
    client = get_client_for_shop(shop)
    if client is None:
        return []

    response = client.get(
        "products.json", params={"collection_id": collection_id, "limit": str(limit)}
    )
    if not response:
        return []

    results = []
    for raw in response.get("products", []):
        product = _normalize_product(raw, client)
        if product:
            results.append(product)
    return results


def select_variant(
    product_id: int, variant_attributes: str, shop: Optional[str] = None
) -> Optional[VariantMatch]:
    """Select a specific verified variant of a product matching free-text attributes.

    Args:
        product_id: Shopify product ID.
        variant_attributes: Free text describing the desired variant
            (e.g. "red", "large", "6 yard").
        shop: Shop domain (defaults to configured default shop).

    Returns:
        The best-matching ``VariantMatch``, or None if no product/variant found.
    """
    product = get_product_details(product_id, shop=shop)
    if not product or not product.variants:
        return None

    normalized_query = variant_attributes.lower()
    for variant in product.variants:
        if variant.title.lower() in normalized_query or normalized_query in variant.title.lower():
            return variant

    # Fall back to the first available variant
    for variant in product.variants:
        if variant.available:
            return variant

    return product.variants[0]


# --------------------------------------------------------------------------
# Smart filter extraction from free-form customer text
# --------------------------------------------------------------------------

def extract_search_filters(text: str) -> Dict[str, Optional[str]]:
    """Extract simple product-search filters from free-form customer text.

    This is a lightweight heuristic extractor (not NLP-grade) used to decide
    which Shopify filters to apply before calling ``search_products``.

    Args:
        text: Sanitized customer message.

    Returns:
        Dict with optional keys: max_budget, min_budget, color, fabric,
        occasion, category.
    """
    normalized = text.lower()
    filters: Dict[str, Optional[str]] = {
        "max_budget": None,
        "min_budget": None,
        "color": None,
        "fabric": None,
        "occasion": None,
        "category": None,
    }

    max_match = re.search(
        r"(?:under|below|less than|budget\s*)\s*(?:rs\.?|inr|₹)?\s*(\d{3,6})", normalized
    )
    if max_match:
        filters["max_budget"] = max_match.group(1)

    min_match = re.search(r"(?:above|over|more than)\s*(?:rs\.?|inr|₹)?\s*(\d{3,6})", normalized)
    if min_match:
        filters["min_budget"] = min_match.group(1)

    range_match = re.search(
        r"(?:rs\.?|inr|₹)?\s*(\d{3,6})\s*(?:-|to)\s*(?:rs\.?|inr|₹)?\s*(\d{3,6})", normalized
    )
    if range_match:
        filters["min_budget"] = range_match.group(1)
        filters["max_budget"] = range_match.group(2)

    colors = [
        "red", "blue", "green", "yellow", "pink", "black", "white",
        "maroon", "purple", "orange", "gold", "silver", "beige", "cream",
    ]
    for c in colors:
        if c in normalized:
            filters["color"] = c
            break

    fabrics = ["cotton", "silk", "banarasi", "pashmina", "georgette", "chiffon", "linen"]
    for f in fabrics:
        if f in normalized:
            filters["fabric"] = f
            break

    occasions = ["wedding", "festive", "party", "casual", "office", "daily", "engagement", "reception"]
    for o in occasions:
        if o in normalized:
            filters["occasion"] = o
            break

    categories = ["saree", "sarees", "ethnic wear", "ethnic"]
    for cat in categories:
        if cat in normalized:
            filters["category"] = "Sarees" if "saree" in cat else "Ethnic Wear"
            break

    return filters
