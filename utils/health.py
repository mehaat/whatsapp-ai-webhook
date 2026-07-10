"""
utils/health.py
----------------
Health, liveness and readiness reporting for ME-HAAT Fashion AI Bot v4.0.

These checks are intentionally cheap and side-effect free — they inspect
configuration and (optionally) ping the database, but never call external
WhatsApp / Gemini / Shopify APIs, so they are safe to hit frequently from an
uptime monitor or an orchestrator probe.
"""

from __future__ import annotations

from typing import Any, Dict

from config import config
from utils.logging import logger

SERVICE_NAME = "ME-HAAT Fashion AI Bot"


def _shops_connected() -> int:
    try:
        from shopify.auth import token_store

        return len(token_store.list_shops())
    except Exception as exc:  # noqa: BLE001
        logger.debug("HEALTH | shop count unavailable: %s", exc)
        return 0


def _component_status() -> Dict[str, Any]:
    """Report readiness of each subsystem based on configuration presence."""
    shops = _shops_connected()
    return {
        "whatsapp": "configured"
        if config.whatsapp_token and config.phone_number_id
        else "missing_config",
        "gemini": "configured" if config.gemini_api_key else "missing_config",
        "shopify_oauth": "configured"
        if config.shopify_api_key and config.shopify_api_secret
        else "missing_config",
        "shopify_store": "connected"
        if (shops or config.default_shop_domain)
        else "not_installed",
        "whatsapp_catalog": "connected" if config.whatsapp_catalog_id else "text_fallback",
        "database": _database_status(),
    }


def _database_status() -> str:
    """Return a short database status string without raising."""
    if not config.use_database:
        return "disabled"
    try:
        from database import database_healthy

        return "ok" if database_healthy() else "error"
    except Exception as exc:  # noqa: BLE001
        logger.debug("HEALTH | database check unavailable: %s", exc)
        return "unavailable"


def build_health_report() -> Dict[str, Any]:
    """Full health report (backward-compatible superset of the v3.0 payload)."""
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": config.version,
        "shops_connected": _shops_connected(),
        "components": _component_status(),
    }


def liveness() -> Dict[str, Any]:
    """Minimal liveness signal — the process is up."""
    return {"status": "alive", "service": SERVICE_NAME, "version": config.version}


def readiness() -> Dict[str, Any]:
    """Readiness signal — required configuration is present to serve traffic."""
    missing = config.required_vars_present()
    db_status = _database_status()
    ready = not missing and db_status not in {"error", "unavailable"}
    return {
        "ready": ready,
        "version": config.version,
        "missing_env": missing,
        "database": db_status,
        "components": _component_status(),
    }
