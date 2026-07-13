"""
shipping/factory.py
--------------------
Provider selection for the v7.0 fulfilment & shipping system.

``get_provider`` resolves a courier by explicit name or by
``config.shipping_provider``, falling back to the always-available
:class:`ManualProvider` (with a warning) for unknown names. This mirrors the
``payments.factory`` selection pattern.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from config import config
from utils.logging import logger

from shipping.base import ShippingProvider
from shipping.delhivery import DelhiveryProvider
from shipping.manual import ManualProvider
from shipping.shiprocket import ShiprocketProvider

# Registry of provider name -> provider class.
PROVIDERS: Dict[str, Type[ShippingProvider]] = {
    "manual": ManualProvider,
    "shiprocket": ShiprocketProvider,
    "delhivery": DelhiveryProvider,
}


def get_provider(name: Optional[str] = None) -> ShippingProvider:
    """Return a shipping provider instance by name or from config.

    Args:
        name: Explicit provider name. When falsy, ``config.shipping_provider``
            is used. Unknown names fall back to :class:`ManualProvider`.

    Returns:
        An instantiated :class:`ShippingProvider`.
    """
    resolved = (name or getattr(config, "shipping_provider", "") or "manual").strip().lower()
    provider_cls = PROVIDERS.get(resolved)
    if provider_cls is None:
        logger.warning(
            "SHIPPING | unknown provider %r; falling back to manual", resolved
        )
        return ManualProvider()
    return provider_cls()


def available_providers() -> List[str]:
    """Return the list of registered provider names."""
    return list(PROVIDERS.keys())
