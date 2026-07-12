"""
commerce
--------
ME-HAAT Fashion AI Bot v6.0 Enterprise Commerce package.

Houses the WhatsApp Commerce platform: catalog-order ingestion, the order
lifecycle service, Shopify draft-order automation, stock validation, order
tracking, PDF invoices, and customer notifications.

Import-safe: pulling in this package never triggers network or database work.
Persistence is initialized once at startup via ``bootstrap_commerce()``.
"""

from __future__ import annotations

from config import config
from utils.logging import logger


def is_enabled() -> bool:
    """True when the v6 commerce surface is switched on (default)."""
    return bool(config.commerce_enabled)


def bootstrap() -> bool:
    """Ensure commerce persistence exists. Safe/idempotent; never raises."""
    if not is_enabled():
        logger.info("COMMERCE | Disabled (COMMERCE_ENABLED=false); running as v5.1")
        return False
    try:
        from database.migrations import bootstrap_commerce

        ok = bootstrap_commerce()
        if ok:
            logger.info("COMMERCE | v6.0 Enterprise Commerce ready")
        return ok
    except Exception as exc:  # noqa: BLE001 - never crash startup
        logger.error("COMMERCE | bootstrap failed (continuing degraded): %s", exc)
        return False
