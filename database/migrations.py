"""
database/migrations.py
-----------------------
Lightweight, dependency-free schema management for ME-HAAT Fashion AI Bot v6.0.

The v6 commerce surface needs durable persistence, so its tables are created on
startup regardless of ``USE_DATABASE``. Rather than pull in Alembic (heavy for
this single-service deployment), we run two idempotent steps that together give
safe *automatic migration* for the additive changes this app makes:

    1. ``create_all`` — creates any table that does not yet exist.
    2. ``ensure_columns`` — for tables that already exist, adds any model column
       that is missing via ``ALTER TABLE ... ADD COLUMN`` (works on both SQLite
       and PostgreSQL for simple additive columns).

Both steps are safe to run on every boot and on both SQLite and PostgreSQL.
Destructive changes (drops/renames/type changes) are intentionally *not*
performed automatically; they would need a real migration.
"""

from __future__ import annotations

from typing import List

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.types import Boolean, DateTime, Integer, Numeric

from utils.logging import logger

_bootstrapped = False


def _sql_type_for(column) -> str:
    """Best-effort SQL type string for an additive ALTER TABLE ADD COLUMN."""
    try:
        ctype = column.type
        if isinstance(ctype, Integer):
            return "INTEGER"
        if isinstance(ctype, Numeric):
            return "NUMERIC"
        if isinstance(ctype, Boolean):
            return "BOOLEAN"
        if isinstance(ctype, DateTime):
            return "TIMESTAMP"
        # String/Text and anything else -> portable TEXT.
        return "TEXT"
    except Exception:  # noqa: BLE001
        return "TEXT"


def ensure_columns(engine: Engine, base) -> List[str]:
    """Add any model columns missing from already-existing tables.

    Returns the list of ``table.column`` names that were added. Never raises for
    an individual column — a failure on one column is logged and skipped so the
    rest of startup proceeds.
    """
    added: List[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in base.metadata.sorted_tables:
        if table.name not in existing_tables:
            # create_all handles brand-new tables; nothing to reconcile here.
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_cols:
                continue
            col_type = _sql_type_for(column)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
                logger.info("MIGRATION | Added column %s.%s (%s)", table.name, column.name, col_type)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MIGRATION | Could not add column %s.%s: %s", table.name, column.name, exc
                )
    return added


def bootstrap_commerce(force: bool = False) -> bool:
    """Create/upgrade all tables needed by the v6 commerce platform.

    Idempotent and safe on every boot. Returns True on success, False if the
    database backend is unavailable (the app then runs without commerce
    persistence rather than crashing).
    """
    global _bootstrapped
    if _bootstrapped and not force:
        return True
    try:
        from database.db import Base, get_engine
        from database import models  # noqa: F401 - register all tables on Base

        engine = get_engine()
        Base.metadata.create_all(engine)
        ensure_columns(engine, Base)
        _bootstrapped = True
        logger.info("MIGRATION | Commerce schema ensured (create_all + column reconcile)")
        return True
    except Exception as exc:  # noqa: BLE001 - never crash startup on DB issues
        logger.error("MIGRATION | Commerce bootstrap failed (continuing degraded): %s", exc)
        return False
