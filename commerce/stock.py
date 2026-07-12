"""
commerce/stock.py
------------------
Stock validation for incoming WhatsApp catalog orders (v6.0).

WhatsApp catalog items reference a ``product_retailer_id`` that a merchant maps
to a Shopify variant. That mapping is deployment-specific, so stock enforcement
is **opt-in** via ``STOCK_VALIDATION_ENABLED`` and *fails open*: when validation
is disabled, or when a variant cannot be resolved / Shopify is unreachable, the
order is allowed through (and the situation is logged) rather than blocking a
real customer. When enabled and a variant is resolvable and out of stock, the
item is reported unavailable so the webhook can reply with the out-of-stock
message the spec requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from config import config
from utils.logging import logger


@dataclass
class StockResult:
    """Outcome of validating a set of order items against live inventory."""

    ok: bool
    unavailable: List[str] = field(default_factory=list)  # product identifiers
    checked: bool = False  # True when a real inventory check actually ran


def resolve_variant_id(item) -> Optional[int]:
    """Best-effort resolution of a Shopify variant id from an order item.

    Accepts either a :class:`commerce.schema.ParsedItem` or a plain dict. Tries,
    in order: an explicit ``variant_id``; a fully-numeric ``product_retailer_id``;
    or the last numeric group inside a compound retailer id such as
    ``shopify_IN_123_456`` (-> 456).
    """
    variant_id = _get(item, "variant_id")
    if variant_id and str(variant_id).isdigit():
        return int(variant_id)

    retailer_id = str(_get(item, "product_retailer_id") or "")
    if retailer_id.isdigit():
        return int(retailer_id)

    nums = re.findall(r"\d+", retailer_id)
    if nums:
        return int(nums[-1])
    return None


def validate_stock(items: List, shop: Optional[str] = None) -> StockResult:
    """Validate every item against Shopify inventory (when enforcement is on).

    Returns a :class:`StockResult`. When ``STOCK_VALIDATION_ENABLED`` is false,
    returns ``ok=True, checked=False`` immediately (fail-open).
    """
    if not config.stock_validation_enabled:
        return StockResult(ok=True, checked=False)

    try:
        from shopify.inventory import check_variant_inventory
    except Exception as exc:  # noqa: BLE001
        logger.warning("STOCK | inventory module unavailable, allowing order: %s", exc)
        return StockResult(ok=True, checked=False)

    unavailable: List[str] = []
    checked_any = False

    for item in items:
        variant_id = resolve_variant_id(item)
        label = str(_get(item, "product_name") or _get(item, "product_retailer_id") or "item")
        qty = int(_get(item, "quantity") or 1)

        if variant_id is None:
            # Cannot map to a Shopify variant -> fail open for this line.
            logger.info("STOCK | Could not resolve variant for '%s'; allowing", label)
            continue

        try:
            status = check_variant_inventory(variant_id, shop=shop)
        except Exception as exc:  # noqa: BLE001
            logger.warning("STOCK | Inventory check failed for %s (%s); allowing", variant_id, exc)
            continue

        if status is None:
            # Variant not found / shop not connected -> fail open.
            logger.info("STOCK | No inventory record for variant %s; allowing", variant_id)
            continue

        checked_any = True
        if not status.available or (status.quantity is not None and status.quantity < qty):
            unavailable.append(status.product_title or label)

    return StockResult(ok=not unavailable, unavailable=unavailable, checked=checked_any)


def _get(item, key: str):
    """Read ``key`` from either a dataclass/object or a dict."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)
