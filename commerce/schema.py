"""
commerce/schema.py
-------------------
Plain data contracts for the v6.0 commerce flow. These decouple the parsing
and service layers from the SQLAlchemy ORM so the rest of the app (webhook,
API, notifications, invoices) works with simple, serializable structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional


def to_decimal(value: Any, default: str = "0") -> Decimal:
    """Coerce an arbitrary numeric-ish value to Decimal, never raising."""
    if value is None or value == "":
        value = default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


@dataclass
class ParsedItem:
    """One line item extracted from a WhatsApp catalog order message."""

    product_retailer_id: str
    quantity: int = 1
    unit_price: Decimal = field(default_factory=lambda: Decimal("0"))
    currency: str = "INR"
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    product_name: Optional[str] = None
    variant: Optional[str] = None

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * Decimal(self.quantity)


@dataclass
class ParsedOrder:
    """A WhatsApp catalog order normalized from the Meta webhook payload."""

    wa_number: str
    wa_order_id: Optional[str] = None
    catalog_id: Optional[str] = None
    customer_name: Optional[str] = None
    currency: str = "INR"
    items: List[ParsedItem] = field(default_factory=list)
    note: Optional[str] = None
    timestamp: Optional[str] = None
    language: Optional[str] = None

    @property
    def subtotal(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal("0"))

    @classmethod
    def from_whatsapp(
        cls,
        message: Dict[str, Any],
        profile_name: str = "",
        default_currency: str = "INR",
    ) -> "ParsedOrder":
        """Build a :class:`ParsedOrder` from a Meta ``type == "order"`` message.

        Meta's order message shape::

            {
              "from": "9199...", "id": "wamid...", "timestamp": "...",
              "type": "order",
              "order": {
                "catalog_id": "...", "text": "note",
                "product_items": [
                  {"product_retailer_id": "SKU", "quantity": 2,
                   "item_price": 1499.0, "currency": "INR"}, ...
                ]
              }
            }
        """
        order = message.get("order", {}) or {}
        raw_items = order.get("product_items", []) or []
        currency = default_currency

        items: List[ParsedItem] = []
        for raw in raw_items:
            item_currency = (raw.get("currency") or default_currency).upper()
            currency = item_currency  # orders are single-currency in practice
            items.append(
                ParsedItem(
                    product_retailer_id=str(raw.get("product_retailer_id", "")),
                    quantity=int(raw.get("quantity", 1) or 1),
                    unit_price=to_decimal(raw.get("item_price", 0)),
                    currency=item_currency,
                    product_id=(str(raw["product_id"]) if raw.get("product_id") else None),
                )
            )

        return cls(
            wa_number=str(message.get("from", "")),
            wa_order_id=str(message.get("id", "")) or None,
            catalog_id=(str(order["catalog_id"]) if order.get("catalog_id") else None),
            customer_name=profile_name or None,
            currency=currency,
            items=items,
            note=(order.get("text") or None),
            timestamp=(str(message["timestamp"]) if message.get("timestamp") else None),
        )
