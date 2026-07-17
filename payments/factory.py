"""
payments/factory.py
--------------------
Provider selection for the v6.0 payment system.

``get_provider`` resolves a provider by explicit name or by
``config.payment_provider``, falling back to the always-available Manual UPI
provider (with a warning) for unknown names.
"""

from __future__ import annotations

from typing import Dict, Optional, Type

from config import config
from utils.logging import logger

from payments.base import PaymentProvider
from payments.cashfree_provider import CashfreeProvider
from payments.manual_upi import ManualUpiProvider
from payments.phonepe_provider import PhonePeProvider
from payments.razorpay_provider import RazorpayProvider
from payments.stripe_provider import StripeProvider

# Registry of provider name -> provider class.
PROVIDERS: Dict[str, Type[PaymentProvider]] = {
    "manual_upi": ManualUpiProvider,
    "razorpay": RazorpayProvider,
    "stripe": StripeProvider,
    "cashfree": CashfreeProvider,
    "phonepe": PhonePeProvider,
}


def get_provider(name: Optional[str] = None) -> PaymentProvider:
    """Return a payment provider instance by name or from config.

    Args:
        name: Explicit provider name. When falsy, ``config.payment_provider``
            is used. Unknown names fall back to :class:`ManualUpiProvider`.

    Returns:
        An instantiated :class:`PaymentProvider`.
    """
    resolved = (name or config.payment_provider or "manual_upi").strip().lower()
    provider_cls = PROVIDERS.get(resolved)
    if provider_cls is None:
        logger.warning(
            "PAYMENTS | unknown provider %r; falling back to manual_upi", resolved
        )
        return ManualUpiProvider()
    return provider_cls()
