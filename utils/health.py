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

import os
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


# --------------------------------------------------------------------------- #
# v10.1: richer, still-cheap probes. Every probe is fully guarded: a failure
# yields a safe {"ok": False, "error": ...} sub-dict and NEVER raises, so the
# /health endpoint can never be blocked or crashed by a probe. No probe makes an
# external network call (no Gemini / Shopify / WhatsApp).
# --------------------------------------------------------------------------- #

def _database_probe() -> Dict[str, Any]:
    """Cheap database inspection (backend/integrity/reachability), backend-aware.

    On SQLite this reports the file path/size and a ``PRAGMA quick_check``; on
    PostgreSQL (Neon/Render) it reports the backend and a ``SELECT 1``
    reachability check (no file path applies). Fully guarded — never raises.
    """
    try:
        from database.db import backend_name, get_engine, is_sqlite
        from sqlalchemy import text

        info: Dict[str, Any] = {
            "backend": backend_name(),
            "integrity": "unknown",
            "reachable": False,
        }
        if is_sqlite():
            from utils.dbpath import canonical_sqlite_path, database_size_bytes

            path = canonical_sqlite_path()
            size = database_size_bytes()
            info.update(
                path=path,
                size_bytes=size,
                size_mb=round(size / (1024 * 1024), 3),
            )
            with get_engine().connect() as conn:
                row = conn.exec_driver_sql("PRAGMA quick_check;").fetchone()
            info["integrity"] = str(row[0]) if row else "unknown"
            info["reachable"] = True
        else:
            # Server backend: a trivial round-trip is the reachability signal.
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            info["integrity"] = "ok"
            info["reachable"] = True
        return info
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _oauth_probe() -> Dict[str, Any]:
    """OAuth token store summary (shop count + most-recent install/update)."""
    info: Dict[str, Any] = {"token_count": 0, "shops": [], "last_oauth": None}
    try:
        from shopify.auth import token_store

        shops = list(token_store.list_shops())
        info["token_count"] = len(shops)
        info["shops"] = shops
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "token_count": 0, "shops": [],
                "last_oauth": None}

    # Best-effort, cheap: most recent installed_at/updated_at if the table exists.
    try:
        from sqlalchemy import func

        from database.db import session_scope
        from database.models_admin import ShopToken

        with session_scope() as session:
            latest_install = session.query(func.max(ShopToken.installed_at)).scalar()
            latest_update = session.query(func.max(ShopToken.updated_at)).scalar()
        candidates = [v for v in (latest_install, latest_update) if v is not None]
        info["last_oauth"] = max(candidates) if candidates else None
    except Exception:  # noqa: BLE001 - table may not exist yet; non-fatal
        info["last_oauth"] = None
    return info


def _dashboard_probe() -> Dict[str, Any]:
    """Confirm the admin dashboard schema exists on the active backend."""
    try:
        from sqlalchemy import inspect

        from database.db import get_engine

        inspector = inspect(get_engine())
        reachable = inspector.has_table("dash_conversations") or inspector.has_table(
            "dash_customers"
        )
        return {"reachable": bool(reachable)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "reachable": False}


def _conversation_memory_probe() -> Dict[str, Any]:
    """Count active in-memory conversation sessions (None if unobtainable)."""
    try:
        from memory.store import conversation_memory

        with conversation_memory._lock:  # noqa: SLF001 - cheap, read-only count
            count = len(conversation_memory._last_seen)  # noqa: SLF001
        return {"active_sessions": count}
    except Exception as exc:  # noqa: BLE001
        return {"active_sessions": None, "error": str(exc)}


def _observability_extras() -> Dict[str, Any]:
    """Assemble the additive v10.1 richer view (all cheap, all guarded)."""
    db = _database_probe()
    oauth = _oauth_probe()
    token_count = oauth.get("token_count", 0)
    return {
        "database": db,
        "oauth": oauth,
        "shopify": {
            "configured": bool(
                config.shopify_api_key
                and config.shopify_api_secret
                and config.shopify_app_url
            ),
            "installed_shops": token_count,
        },
        "whatsapp": {
            "configured": bool(
                config.verify_token
                and config.whatsapp_token
                and config.phone_number_id
            )
        },
        "gemini": {
            "configured": bool(config.gemini_api_key),
            "model": config.gemini_model,
        },
        "dashboard": _dashboard_probe(),
        "conversation_memory": _conversation_memory_probe(),
    }


def build_health_report() -> Dict[str, Any]:
    """Full health report (backward-compatible superset of the v3.0 payload).

    v10.1 keeps every existing top-level key (status/service/version/
    shops_connected/components) and ADDS a richer, still-cheap view (database,
    oauth, shopify, whatsapp, gemini, dashboard, conversation_memory). All added
    probes are guarded and never raise or block the endpoint.
    """
    report: Dict[str, Any] = {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": config.version,
        "shops_connected": _shops_connected(),
        "components": _component_status(),
    }
    try:
        report.update(_observability_extras())
    except Exception as exc:  # noqa: BLE001 - additive view must never break /health
        logger.debug("HEALTH | observability extras unavailable: %s", exc)
        report["observability_error"] = str(exc)
    return report


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
