"""
shopify/search.py
------------------
Verified product search and catalog lookups for ME-HAAT Fashion AI Bot v4.0.

All product facts (price, stock, variants, collections) are sourced live
from the Shopify Admin API via ``ShopifyClient`` — the AI layer must never
invent this data.

v4.0 additions (backward compatible):
    - ``ProductMatch.short_description`` + ``ProductMatch.to_card_dict()`` so the
      WhatsApp layer can render rich product cards.
    - ``detect_product_search_intent()`` to reliably recognise "show saree",
      "red silk saree", "under 3000", "party wear", etc.
    - ``search_and_rank()`` — a single high-level entry point that combines
      title + tags + product type + filters, ranks results by relevance,
      de-duplicates, and returns at most ``limit`` products.

Every pre-existing function keeps its exact signature and behaviour.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from shopify.client import ShopifyClient, get_client_for_shop
from shopify.auth import token_store
from utils.logging import logger

# Currency symbols used when rendering verified prices. Falls back to the raw
# ISO code for anything not listed here.
CURRENCY_SYMBOLS: Dict[str, str] = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "AED": "د.إ",
}


def currency_symbol(code: str) -> str:
    """Return a display symbol for a currency ISO code (e.g. ``INR`` -> ``₹``)."""
    return CURRENCY_SYMBOLS.get((code or "").upper(), code or "")


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(raw_html: str, max_length: int = 160) -> str:
    """Convert a Shopify ``body_html`` blob into a short plain-text description."""
    if not raw_html:
        return ""
    text = _HTML_TAG_RE.sub(" ", raw_html)
    text = (
        text.replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


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
    short_description: str = ""
    handle: str = ""

    @property
    def variant_count(self) -> int:
        """Number of verified variants for this product."""
        return len(self.variants)

    @property
    def stock_label(self) -> str:
        """Human-readable stock label."""
        return "In Stock" if self.in_stock else "Out of Stock"

    def to_context_line(self) -> str:
        """Render this product as a single verified-context line for Gemini.

        NOTE: The leading fields are unchanged from v3.0 for backward
        compatibility; the product URL is appended at the end.
        """
        return (
            f"- [ID:{self.product_id}] {self.title} | "
            f"Price: {self.currency} {self.price} | "
            f"{self.stock_label} | Category: {self.product_type} | "
            f"Variants: {self.variant_count} | URL: {self.url or 'n/a'}"
        )

    def to_card_dict(self) -> Dict[str, object]:
        """Render this product as a dict consumable by ``send_product_card``.

        Keys are a superset of the v3.0 contract (title, price, currency,
        stock_label, url) so older callers keep working.
        """
        return {
            "product_id": self.product_id,
            "title": self.title,
            "price": self.price,
            "currency": self.currency,
            "currency_symbol": currency_symbol(self.currency),
            "in_stock": self.in_stock,
            "stock_label": self.stock_label,
            "product_type": self.product_type,
            "variant_count": self.variant_count,
            "short_description": self.short_description,
            "url": self.url,
            "handle": self.handle,
            # Native WhatsApp catalog messages reference items by retailer id;
            # by Shopify convention this is typically the product/variant id.
            "retailer_id": str(self.product_id),
        }


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
        short_description=_strip_html(raw.get("body_html", "")),
        handle=handle,
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

# Vocabulary shared by the filter extractor and the intent detector so the two
# never drift out of sync.
_COLORS = [
    "red", "blue", "green", "yellow", "pink", "black", "white",
    "maroon", "purple", "orange", "gold", "silver", "beige", "cream",
    "navy", "teal", "peach", "mustard", "grey", "gray",
]
_FABRICS = [
    "cotton", "silk", "banarasi", "pashmina", "georgette", "chiffon",
    "linen", "organza", "chanderi", "tussar", "kanjivaram", "kanchipuram",
]
_OCCASIONS = [
    "wedding", "festive", "festival", "party", "casual", "office", "daily",
    "engagement", "reception", "bridal", "partywear",
]
_PRODUCT_NOUNS = [
    "saree", "sarees", "sari", "saris", "lehenga", "lehngas", "lehenga choli",
    "kurti", "kurtis", "suit", "suits", "salwar", "anarkali", "gown", "gowns",
    "dupatta", "ethnic wear", "ethnic", "dress", "dresses", "blouse",
]
_SHOW_VERBS = [
    "show", "dikhao", "dikha", "dekhna", "dekhao", "dikhaye", "batao",
    "chahiye", "want", "looking for", "need", "browse", "buy", "recommend",
    "suggest", "options", "koi", "any",
]


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

    # Range must be checked before under/above so "2000 to 5000" wins cleanly.
    range_match = re.search(
        r"(?:rs\.?|inr|₹)?\s*(\d{3,6})\s*(?:-|to|se)\s*(?:rs\.?|inr|₹)?\s*(\d{3,6})", normalized
    )
    if range_match:
        filters["min_budget"] = range_match.group(1)
        filters["max_budget"] = range_match.group(2)
    else:
        max_match = re.search(
            r"(?:under|below|less than|upto|up to|within|budget\s*)\s*"
            r"(?:rs\.?|inr|₹)?\s*(\d{3,6})",
            normalized,
        )
        if max_match:
            filters["max_budget"] = max_match.group(1)

        min_match = re.search(
            r"(?:above|over|more than|starting|minimum)\s*(?:rs\.?|inr|₹)?\s*(\d{3,6})",
            normalized,
        )
        if min_match:
            filters["min_budget"] = min_match.group(1)

    for c in _COLORS:
        if re.search(r"\b" + re.escape(c) + r"\b", normalized):
            filters["color"] = "grey" if c == "gray" else c
            break

    for f in _FABRICS:
        if f in normalized:
            filters["fabric"] = f
            break

    for o in _OCCASIONS:
        if o in normalized:
            filters["occasion"] = "party" if o == "partywear" else o
            break

    # Category maps to a Shopify product_type. We keep the same values v3.0 used.
    if re.search(r"\bsaree|sari\b", normalized) or "saree" in normalized or "sari" in normalized:
        filters["category"] = "Sarees"
    elif "ethnic" in normalized:
        filters["category"] = "Ethnic Wear"

    return filters


def detect_product_search_intent(text: str) -> bool:
    """Return True if the customer is trying to browse / search for products.

    Recognises, e.g.:
        "Show saree", "silk saree", "red saree", "wedding saree",
        "party wear", "under 3000", "above 5000", "blue silk saree",
        "banarasi saree", "koi saree dikhao", "suits under 2000".

    This is intentionally broader than ``extract_search_filters`` returning a
    signal, because a bare product noun ("saree") should also trigger a live
    search rather than the static catalogue link.
    """
    if not text:
        return False
    normalized = text.lower()

    # Any concrete product noun is a strong signal.
    if any(re.search(r"\b" + re.escape(noun) + r"\b", normalized) for noun in _PRODUCT_NOUNS):
        return True

    # In an ethnic-wear store, naming a price, a fabric, or an occasion is
    # itself a browse intent. A bare colour is ambiguous, so it needs a
    # show/want verb alongside it.
    filters = extract_search_filters(normalized)
    has_price = bool(filters["max_budget"] or filters["min_budget"])
    has_verb = any(v in normalized for v in _SHOW_VERBS)

    if has_price:
        return True
    if filters["fabric"] or filters["occasion"]:
        return True
    if filters["color"] and has_verb:
        return True
    return False


def _relevance_score(product: ProductMatch, terms: List[str], filters: Dict[str, Optional[str]]) -> float:
    """Score a product for how well it matches the customer's request.

    Higher is better. Combines title, tags and product type against the
    extracted terms and filters, and lightly favours in-stock items.
    """
    haystack = (
        f"{product.title.lower()} "
        f"{product.product_type.lower()} "
        f"{' '.join(product.tags)}"
    )
    score = 0.0
    for term in terms:
        if not term:
            continue
        if term in product.title.lower():
            score += 3.0
        elif term in product.product_type.lower():
            score += 2.0
        elif term in haystack:
            score += 1.5

    for key in ("color", "fabric", "occasion"):
        value = filters.get(key)
        if value and value in haystack:
            score += 2.0

    if product.in_stock:
        score += 1.0
    return score


def search_and_rank(
    text: str,
    shop: Optional[str] = None,
    limit: int = 5,
) -> List[ProductMatch]:
    """High-level product search used by the WhatsApp orchestration layer.

    Combines title + tags + product type + extracted filters, ranks the
    results by relevance, removes duplicates, and returns at most ``limit``
    verified products (most relevant first).

    Args:
        text: The sanitized customer message.
        shop: Optional explicit shop domain.
        limit: Maximum number of products to return.

    Returns:
        A de-duplicated, relevance-sorted list of ``ProductMatch`` objects.
    """
    started = time.perf_counter()
    filters = extract_search_filters(text)

    max_budget = _to_float(filters.get("max_budget"))
    min_budget = _to_float(filters.get("min_budget"))

    # Build the free-text query from the most descriptive tokens.
    query_terms = [
        v for v in (filters.get("fabric"), filters.get("color")) if v
    ]
    query_text = " ".join(query_terms)

    token_present = bool(token_store.get_default_shop() or shop)
    logger.info(
        "SHOPIFY SEARCH START | query=%r | filters=%s | oauth_token=%s",
        text,
        {k: v for k, v in filters.items() if v},
        "present" if token_present else "MISSING",
    )

    # Pull a generous candidate set (we re-rank locally), then also try a
    # category-only pass so a bare "show saree" still returns items even when
    # the fabric/colour query would have been too narrow.
    candidates: List[ProductMatch] = search_products(
        shop=shop,
        query_text=query_text,
        max_budget=max_budget,
        min_budget=min_budget,
        color=filters.get("color"),
        fabric=filters.get("fabric"),
        occasion=filters.get("occasion"),
        category=filters.get("category"),
        limit=limit * 3,
    )

    if not candidates and (query_text or filters.get("category")):
        candidates = search_products(
            shop=shop,
            max_budget=max_budget,
            min_budget=min_budget,
            category=filters.get("category"),
            limit=limit * 3,
        )

    # De-duplicate by product id (Task 9) while preserving first occurrence.
    seen: set = set()
    unique: List[ProductMatch] = []
    for p in candidates:
        if p.product_id in seen:
            continue
        seen.add(p.product_id)
        unique.append(p)

    ranked = sorted(
        unique,
        key=lambda p: _relevance_score(p, [t for t in query_terms], filters),
        reverse=True,
    )
    top = ranked[:limit]

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "SHOPIFY SEARCH DONE | products_found=%d | returned=%d | time_ms=%.1f",
        len(unique),
        len(top),
        elapsed_ms,
    )
    return top


def _to_float(value: Optional[str]) -> Optional[float]:
    """Safely convert an optional string to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
