"""
payments/base.py
-----------------
Core contracts for the ME-HAAT Fashion AI Bot v6.0 payment system.

Every concrete provider (Manual UPI, Razorpay, Stripe, Cashfree, PhonePe)
implements :class:`PaymentProvider`. Two immutable value objects flow through
the system:

* :class:`PaymentLink`  — the result of creating a hosted/deep-link checkout.
* :class:`WebhookResult` — the normalized outcome of parsing a provider webhook.

Keeping these contracts free of any provider-specific detail lets the factory
and package facade treat all providers uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Union

from config import config

# Amounts may arrive as Decimal (persisted) or float (order dicts, rupees).
Amount = Union[Decimal, float, int]


@dataclass
class PaymentLink:
    """A payable link produced by a provider.

    Attributes:
        url: The URL (or ``upi://`` deep link) the customer opens to pay.
        provider: The provider name that produced the link.
        amount: The charge amount in *major* units (e.g. rupees).
        currency: ISO currency code (e.g. ``"INR"``).
        provider_link_id: Provider's identifier for the payment link/order.
        provider_payment_id: Provider's identifier for the payment, if known.
        expires_at: UTC-aware expiry time of the link, if any.
        raw: The raw provider response payload for auditing/debugging.
    """

    url: str
    provider: str
    amount: Amount
    currency: str
    provider_link_id: Optional[str] = None
    provider_payment_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class WebhookResult:
    """The normalized outcome of verifying and parsing a provider webhook.

    Attributes:
        ok: True when the webhook signature verified and parsed successfully.
        status: Normalized payment status (``paid``/``failed``/``refunded``/...).
        provider_payment_id: Provider payment id extracted from the event.
        provider_link_id: Provider link/order id extracted from the event.
        order_id: Local order id if the event carried one.
        event: The raw provider event name (e.g. ``payment_link.paid``).
        raw: The parsed JSON body for auditing/debugging.
    """

    ok: bool
    status: str = ""
    provider_payment_id: Optional[str] = None
    provider_link_id: Optional[str] = None
    order_id: Optional[int] = None
    event: str = ""
    raw: Optional[Dict[str, Any]] = None


class PaymentProvider(ABC):
    """Abstract base class every payment provider adapter must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the short, stable provider name (e.g. ``"razorpay"``)."""
        raise NotImplementedError

    @abstractmethod
    def create_link(self, order: Dict[str, Any]) -> PaymentLink:
        """Create a payable link/deep-link for the given order dict.

        Args:
            order: An order dict (see module contract) with at least ``id``,
                ``order_number``, ``currency`` and ``total_amount``.

        Returns:
            A :class:`PaymentLink` the customer can use to pay.
        """
        raise NotImplementedError

    @abstractmethod
    def verify_and_parse_webhook(
        self, headers: Dict[str, str], raw_body: bytes
    ) -> WebhookResult:
        """Verify a provider webhook signature and parse its payload.

        Args:
            headers: The inbound HTTP headers (case-insensitive access assumed).
            raw_body: The exact raw request body bytes (needed for signatures).

        Returns:
            A normalized :class:`WebhookResult`. ``ok`` is False on any
            signature/parse failure.
        """
        raise NotImplementedError

    # -- shared helpers ---------------------------------------------------

    def _expiry(self) -> datetime:
        """Return a UTC-aware expiry ``now + payment_link_expiry_minutes``."""
        minutes = int(getattr(config, "payment_link_expiry_minutes", 1440) or 1440)
        return datetime.now(timezone.utc) + timedelta(minutes=minutes)

    @staticmethod
    def _get_header(headers: Dict[str, str], name: str) -> str:
        """Case-insensitively fetch a header value, returning ``""`` if absent."""
        if not headers:
            return ""
        target = name.lower()
        for key, value in headers.items():
            if key.lower() == target:
                return value or ""
        return ""
