"""
admin/db.py
------------
Persistence access layer for the Admin Dashboard.

Historically this module opened its own ``sqlite3`` connection to a private
database file. It now routes every dashboard query through the **one** shared
SQLAlchemy engine (:mod:`database.db`) via a small portable DBAPI shim
(:mod:`database.compat`), so the dashboard automatically uses whichever backend
``DATABASE_URL`` selects:

    * ``sqlite:///…`` / unset  -> local SQLite file (dev)
    * ``postgres://…``         -> PostgreSQL (Neon / Render)

Crucially, the dashboard's existing hand-written SQL in :mod:`admin.tracker`,
:mod:`admin.analytics` and :mod:`admin.routes` is **unchanged** — the shim
translates ``?`` placeholders to the driver's paramstyle and returns rows that
support both ``row["col"]`` and ``row[0]`` access on any backend. There is no
``sqlite3.connect()`` here anymore.

Public API (unchanged, relied upon across the dashboard):
    get_conn(write: bool = False) -> context manager yielding a connection
    init_db() -> ensure schema + seed admin user (idempotent)

Design notes:
    * The dashboard tables are ORM models (:mod:`database.models_admin`), so the
      schema is created by the unified ``create_all`` on any backend and is
      versioned by Alembic — no more hand-maintained ``CREATE TABLE`` DDL.
    * Writers are serialised on SQLite (one file-writer) by the shim's lock; on
      Postgres the connection pool + MVCC handle concurrency. Calling
      :func:`init_db` on every startup is safe and never destroys data.

Tables (per the dashboard specification):
    users, dash_customers, dash_conversations, messages, ai_history,
    products, product_sends, dash_orders
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from admin.config import admin_config
from database.compat import PortableConnection, portable_conn
from utils.logging import logger

_initialised = False
_init_lock = threading.Lock()


@contextmanager
def get_conn(write: bool = False) -> Iterator[PortableConnection]:
    """Yield a portable connection, committing on success for write operations.

    Backward-compatible with the previous ``sqlite3``-based helper: callers keep
    using ``conn.execute("… ?", params).fetchone()``, ``row["col"]`` / ``row[0]``
    and ``cursor.rowcount`` exactly as before, on SQLite *or* Postgres.

    Args:
        write: When True, commit on clean exit and roll back on error (and, on
            SQLite, serialise writers via the shim's process lock).
    """
    ensure_initialised()
    with portable_conn(write=write) as conn:
        yield conn


def init_db() -> None:
    """Create the dashboard schema if needed and seed the admin user (idempotent).

    Schema creation is delegated to the ONE unified ``create_all`` (the admin
    tables are ORM models registered on the shared ``Base``), so it works on
    SQLite and PostgreSQL alike. The admin ``users`` row is then seeded via the
    portable connection.
    """
    # Ensure every table (incl. the dash_* + users tables) exists on the active
    # backend. Idempotent and safe on every boot.
    from database.db import init_db as _init_all_tables

    _init_all_tables()

    with portable_conn(write=True) as conn:
        _seed_admin_user(conn)

    try:
        from database.db import backend_name

        logger.info("ADMIN | Datastore ready on %s backend", backend_name())
    except Exception:  # noqa: BLE001 - logging must never break startup
        logger.info("ADMIN | Datastore ready")


def _seed_admin_user(conn: PortableConnection) -> None:
    """Ensure a ``users`` row exists for the configured admin (idempotent)."""
    from admin.security import hash_password  # local import avoids cycle

    username = admin_config.username
    if not username:
        return
    existing = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()
    if existing:
        return
    stored_hash = admin_config.password_hash or (
        hash_password(admin_config.password) if admin_config.password else ""
    )
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) "
        "VALUES (?, ?, 'admin', ?)",
        (username, stored_hash, _now_iso()),
    )


def ensure_initialised() -> None:
    """Initialise the schema exactly once per process (thread-safe)."""
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        try:
            init_db()
        finally:
            # Mark initialised even on failure to avoid hammering a broken
            # backend; individual operations still surface their own errors.
            _initialised = True


def _now_iso() -> str:
    """UTC ISO-8601 timestamp (imported lazily to keep this module light)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
