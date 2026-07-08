"""
database
--------
Optional SQLAlchemy persistence layer for ME-HAAT Fashion AI Bot v4.0.

This package is *opt-in*: nothing here runs unless ``USE_DATABASE=true`` and
SQLAlchemy is installed. The public facade below is import-safe even when
SQLAlchemy is missing — every function degrades to a harmless no-op — so the
core WhatsApp/Shopify/Gemini paths can never be broken by the database layer.

Public facade:
    bootstrap_database()   -> create tables on startup (no-op unless enabled)
    log_ai_interaction(..) -> best-effort AI audit log
    database_healthy()     -> bool, used by health checks
"""

from __future__ import annotations

from typing import Optional

from config import config
from utils.logging import logger
from utils.security import mask_pii

# Attempt to load the SQLAlchemy-backed implementation. If SQLAlchemy is not
# installed, ``_BACKEND_OK`` stays False and every facade function no-ops.
try:  # pragma: no cover - depends on optional dependency being installed
    from database.db import healthcheck, init_db, session_scope
    from database import repositories

    _BACKEND_OK = True
except Exception as exc:  # noqa: BLE001
    logger.warning("DATABASE | SQLAlchemy backend unavailable: %s", exc)
    _BACKEND_OK = False


def is_enabled() -> bool:
    """True only when persistence is both configured and importable."""
    return bool(config.use_database and _BACKEND_OK)


def bootstrap_database() -> None:
    """Create the schema on startup. No-op unless enabled."""
    if not config.use_database:
        return
    if not _BACKEND_OK:
        logger.warning(
            "DATABASE | USE_DATABASE=true but SQLAlchemy is not installed; "
            "persistence is disabled. Run: pip install -r requirements.txt"
        )
        return
    try:
        init_db()
        logger.info("DATABASE | Bootstrap complete")
    except Exception as exc:  # noqa: BLE001 - never crash startup on DB issues
        logger.error("DATABASE | Bootstrap failed (continuing without DB): %s", exc)


def log_ai_interaction(
    wa_number: str,
    user_message: str,
    reply: str,
    context: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Persist an AI interaction (best-effort). No-op unless enabled.

    The user message is PII-masked before storage.
    """
    if not is_enabled():
        return
    try:
        with session_scope() as session:
            repositories.AILogRepository(session).create(
                wa_number=wa_number,
                user_message=mask_pii(user_message or ""),
                reply=reply or "",
                context=(context or "")[:8000] or None,
                model=model,
            )
    except Exception as exc:  # noqa: BLE001 - logging must never break the reply path
        logger.debug("DATABASE | log_ai_interaction failed: %s", exc)


def database_healthy() -> bool:
    """Return True if the database answers a trivial query."""
    if not is_enabled():
        return False
    try:
        return bool(healthcheck())
    except Exception as exc:  # noqa: BLE001
        logger.debug("DATABASE | healthcheck failed: %s", exc)
        return False


__all__ = [
    "bootstrap_database",
    "log_ai_interaction",
    "database_healthy",
    "is_enabled",
]
