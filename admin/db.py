"""
admin/db.py
------------
Self-contained SQLite persistence for the Admin Dashboard.

This uses the Python standard-library ``sqlite3`` module only — it has **no**
dependency on the project's optional SQLAlchemy layer — so the dashboard has a
durable, queryable data source whether or not ``USE_DATABASE`` is enabled. The
database file lives on Render's mounted disk by default (see ``admin/config``),
so data survives restarts and is shared across Gunicorn workers.

Design notes:
    * WAL journal mode + a short busy timeout make concurrent reads/writes from
      multiple Gunicorn workers safe.
    * A new connection is opened per operation (sqlite3 connections are not
      thread-safe to share); this is cheap for SQLite and avoids cross-thread
      state. A module-level lock further serialises writers within a process.
    * Schema creation is idempotent (``CREATE TABLE IF NOT EXISTS``), so calling
      :func:`init_db` on every startup is safe and never destroys data.

Tables (per the dashboard specification):
    users, customers, messages, conversations, orders, products, ai_history
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from admin.config import admin_config
from utils.logging import logger

_write_lock = threading.Lock()
_initialised = False
_init_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    """Open a configured SQLite connection with row access by column name."""
    conn = sqlite3.connect(admin_config.db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_conn(write: bool = False) -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection, committing on success for write operations.

    Args:
        write: When True, serialise via the module write-lock and commit on exit.
    """
    ensure_initialised()
    if write:
        _write_lock.acquire()
    conn = _connect()
    try:
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()
        if write:
            _write_lock.release()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    role          TEXT DEFAULT 'admin',
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS dash_customers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_number     TEXT UNIQUE NOT NULL,
    profile_name  TEXT,
    language      TEXT,
    email         TEXT,
    tags          TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_wa ON dash_customers(wa_number);

CREATE TABLE IF NOT EXISTS dash_conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_number       TEXT UNIQUE NOT NULL,
    profile_name    TEXT,
    last_message    TEXT,
    last_direction  TEXT,
    message_count   INTEGER DEFAULT 0,
    unread_count    INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'open',
    started_at      TEXT NOT NULL,
    last_message_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_last ON dash_conversations(last_message_at);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_number   TEXT NOT NULL,
    direction   TEXT NOT NULL,          -- 'in' (customer) | 'out' (bot)
    text        TEXT,
    language    TEXT,
    intent      TEXT,
    latency_ms  INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_wa ON messages(wa_number);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS ai_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_number     TEXT NOT NULL,
    model         TEXT,
    user_message  TEXT,                 -- PII-masked
    prompt_context TEXT,
    response      TEXT,
    latency_ms    INTEGER,
    fallback_used INTEGER DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_wa ON ai_history(wa_number);
CREATE INDEX IF NOT EXISTS idx_ai_created ON ai_history(created_at);

CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_ref   TEXT UNIQUE NOT NULL, -- product id or normalised title
    title         TEXT,
    price         TEXT,
    currency      TEXT,
    times_sent    INTEGER DEFAULT 0,
    last_sent_at  TEXT
);

CREATE TABLE IF NOT EXISTS product_sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_number   TEXT NOT NULL,
    query       TEXT,
    title       TEXT,
    price       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_sends_created ON product_sends(created_at);

CREATE TABLE IF NOT EXISTS dash_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_name        TEXT,
    wa_number         TEXT,
    customer_name     TEXT,
    email             TEXT,
    phone             TEXT,
    financial_status  TEXT,
    fulfillment_status TEXT,
    total_price       TEXT,
    currency          TEXT,
    tracking          TEXT,
    looked_up_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_name ON dash_orders(order_name);
"""


def init_db() -> None:
    """Create the schema if needed and seed the admin user row (idempotent)."""
    directory = os.path.dirname(admin_config.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _seed_admin_user(conn)
        conn.commit()
        logger.info("ADMIN | SQLite datastore ready at %s", admin_config.db_path)
    finally:
        conn.close()


def _seed_admin_user(conn: sqlite3.Connection) -> None:
    """Ensure a ``users`` row exists for the configured admin (schema table 13)."""
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
            # Mark initialised even on failure to avoid hammering a broken path;
            # individual operations will still surface errors via their guards.
            _initialised = True


def _now_iso() -> str:
    """UTC ISO-8601 timestamp (imported lazily to keep this module light)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
