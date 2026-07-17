"""
payments
--------
Provider-adapter payment system for the ME-HAAT Fashion AI Bot v6.0.

This package facade exposes two orchestration functions used by the rest of the
app plus a small discovery helper:

* :func:`generate_payment_link` — create a payable link for an order and
  persist it via ``order_service.record_payment``. Never raises; on any
  provider error it transparently falls back to Manual UPI.
* :func:`handle_webhook` — verify and parse a provider webhook and, on a
  terminal status, sync the order via ``order_service.mark_payment_paid``.
  Never raises.
* :func:`available_providers` — list the registered provider names.

``order_service`` is imported lazily inside the functions to avoid import
cycles between the commerce and payments layers.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

from utils.logging import logger

from payments.base import PaymentLink, PaymentProvider, WebhookResult
from payments.factory import PROVIDERS, get_provider
from payments.manual_upi import ManualUpiProvider

__all__ = [
    "PaymentLink",
    "PaymentProvider",
    "WebhookResult",
    "PROVIDERS",
    "get_provider",
    "generate_payment_link",
    "handle_webhook",
    "available_providers",
]

# Statuses that should trigger an order-side payment sync.
_TERMINAL_STATUSES = {"paid", "failed", "refunded"}


def available_providers() -> List[str]:
    """Return the list of registered provider names."""
    return list(PROVIDERS.keys())


def _serialize_link(order: Dict[str, Any], link: PaymentLink) -> Dict[str, Any]:
    """Persist a created link and return the public result dict.

    Args:
        order: The order dict the link was created for.
        link: The :class:`PaymentLink` produced by a provider.

    Returns:
        A serializable dict describing the payment link.
    """
    # Lazy import to avoid a commerce <-> payments import cycle.
    from commerce.service import order_service

    order_id = order.get("id")
    currency = link.currency or order.get("currency") or "INR"
    amount_decimal = Decimal(str(link.amount if link.amount is not None else 0))
    raw_json = json.dumps(link.raw, default=str) if link.raw is not None else None

    try:
        if order_id is not None:
            order_service.record_payment(
                int(order_id),
                provider=link.provider,
                amount=amount_decimal,
                currency=currency,
                payment_url=link.url,
                provider_link_id=link.provider_link_id,
                provider_payment_id=link.provider_payment_id,
                status="pending",
                expires_at=link.expires_at,
                raw=raw_json,
            )
    except Exception as exc:  # noqa: BLE001 - persistence must not break the link
        logger.error(
            "PAYMENTS | failed to persist payment for order %s: %s", order_id, exc
        )

    return {
        "url": link.url,
        "provider": link.provider,
        "provider_link_id": link.provider_link_id,
        "provider_payment_id": link.provider_payment_id,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "amount": float(link.amount) if link.amount is not None else 0.0,
        "currency": currency,
    }


def generate_payment_link(
    order: Dict[str, Any], provider_name: Optional[str] = None
) -> Dict[str, Any]:
    """Create and persist a payment link for an order.

    Picks the configured (or named) provider, creates a link and persists it.
    On any provider failure this falls back to :class:`ManualUpiProvider` so the
    caller always receives a usable link. This function never raises.

    Args:
        order: The order dict (needs ``id``, ``order_number``, ``currency``,
            ``total_amount``).
        provider_name: Optional explicit provider name; defaults to config.

    Returns:
        A dict with keys ``url``, ``provider``, ``provider_link_id``,
        ``provider_payment_id``, ``expires_at``, ``amount``, ``currency``.
    """
    provider = get_provider(provider_name)
    try:
        link = provider.create_link(order)
        return _serialize_link(order, link)
    except Exception as exc:  # noqa: BLE001 - fall back, never raise to caller
        logger.error(
            "PAYMENTS | provider %s failed for order %s: %s; falling back to manual_upi",
            getattr(provider, "name", provider_name), order.get("order_number"), exc,
        )

    try:
        fallback = ManualUpiProvider()
        link = fallback.create_link(order)
        return _serialize_link(order, link)
    except Exception as exc:  # noqa: BLE001 - last-resort defensive guard
        logger.error(
            "PAYMENTS | manual_upi fallback failed for order %s: %s",
            order.get("order_number"), exc,
        )
        return {
            "url": "",
            "provider": "manual_upi",
            "provider_link_id": None,
            "provider_payment_id": None,
            "expires_at": None,
            "amount": float(order.get("total_amount") or 0),
            "currency": order.get("currency") or "INR",
        }


def handle_webhook(
    provider_name: str, headers: Dict[str, str], raw_body: bytes
) -> Dict[str, Any]:
    """Verify a provider webhook and sync the order on a terminal status.

    This function never raises; on any failure it returns ``ok=False``.

    Args:
        provider_name: The provider whose webhook this is.
        headers: The inbound HTTP headers.
        raw_body: The exact raw request body bytes.

    Returns:
        A dict ``{"ok": bool, "status": str, "order": order_dict_or_None}``.
    """
    try:
        provider = get_provider(provider_name)
        result = provider.verify_and_parse_webhook(headers, raw_body)
    except Exception as exc:  # noqa: BLE001
        logger.error("PAYMENTS | webhook handling failed for %s: %s", provider_name, exc)
        return {"ok": False, "status": "", "order": None}

    if not result.ok:
        return {"ok": False, "status": result.status, "order": None}

    order: Optional[Dict[str, Any]] = None
    if result.status in _TERMINAL_STATUSES:
        try:
            # Lazy import to avoid a commerce <-> payments import cycle.
            from commerce.service import order_service

            raw_json = (
                json.dumps(result.raw, default=str) if result.raw is not None else None
            )
            order = order_service.mark_payment_paid(
                provider_payment_id=result.provider_payment_id,
                provider_link_id=result.provider_link_id,
                order_id=result.order_id,
                status=result.status,
                raw=raw_json,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "PAYMENTS | failed to sync order for %s webhook: %s", provider_name, exc
            )

    return {"ok": result.ok, "status": result.status, "order": order}
