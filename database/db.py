"""
database/db.py
---------------
The **single** SQLAlchemy engine + session layer for ME-HAAT Fashion AI Bot.

Everything that touches the database in this project — the SQLAlchemy commerce
models, the Shopify OAuth token/state store, the Admin Dashboard datastore and
the health probes — now shares the ONE engine, ONE session factory and ONE
declarative ``Base`` defined here. There is no other engine and no direct
``sqlite3.connect()`` anywhere in the codebase.

Backend selection is automatic and driven only by ``DATABASE_URL``:

    * ``postgres://…`` / ``postgresql://…``  -> PostgreSQL (Neon, Render, …)
    * ``sqlite:///…`` or *unset*             -> local SQLite file

Render's/Neon's legacy ``postgres://`` scheme is normalised to the
``postgresql+psycopg`` dialect (psycopg 3) automatically, so a managed-Postgres
deployment works with no code changes — only the environment variable.

This module imports SQLAlchemy at load time; the ``database`` package guards
that import so the wider app never fails if SQLAlchemy is somehow absent.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TypeVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import config
from utils.logging import logger

T = TypeVar("T")


class Base(DeclarativeBase):
    """The one declarative base every ORM model in the project registers on."""


_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None


# --------------------------------------------------------------------------- #
# URL normalisation
# --------------------------------------------------------------------------- #
def normalize_database_url(url: str) -> str:
    """Normalise a ``DATABASE_URL`` for SQLAlchemy 2.0 + psycopg 3.

    Render/Heroku/Neon frequently hand out bare ``postgres://`` URLs, which
    SQLAlchemy 2.0 rejects with ``NoSuchModuleError``. We rewrite the scheme to
    the ``postgresql+psycopg`` dialect (psycopg 3 — the driver with reliable
    Python 3.14 wheels and first-class Neon support).

    An explicit driver the operator already chose is respected:
        * ``postgresql+psycopg://``   -> kept (psycopg 3)
        * ``postgresql+psycopg2://``  -> kept (psycopg 2, if they pinned it)
        * ``postgres://``             -> ``postgresql+psycopg://``
        * ``postgresql://``           -> ``postgresql+psycopg://``

    Non-Postgres URLs (sqlite, mysql, …) are returned unchanged.
    """
    if not url:
        return url
    stripped = url.strip()
    if stripped.startswith("postgresql+"):
        # Operator picked a driver explicitly — respect it.
        return stripped
    if stripped.startswith("postgres://"):
        return "postgresql+psycopg://" + stripped[len("postgres://"):]
    if stripped.startswith("postgresql://"):
        return "postgresql+psycopg://" + stripped[len("postgresql://"):]
    return stripped


def _resolved_url() -> str:
    """Return the effective, normalised database URL for this process.

    For SQLite this pins the ONE canonical absolute ``mehaat.db`` (via
    :mod:`utils.dbpath`) so tokens, admin data and commerce data always share a
    single file and a changing CWD can never repoint the database. For any
    server backend (Postgres/MySQL) the configured URL is used directly.
    """
    url = normalize_database_url(config.database_url)
    if url.startswith("sqlite"):
        try:
            from utils.dbpath import canonical_sqlite_url

            url = canonical_sqlite_url()
        except Exception as exc:  # noqa: BLE001 - fall back to the raw URL
            logger.warning("DATABASE | canonical sqlite path unavailable: %s", exc)
    return url


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def get_engine() -> Engine:
    """Lazily create (once) and return the single SQLAlchemy engine.

    Pooling defaults differ by backend:

        * SQLite  — ``check_same_thread=False`` plus WAL journalling and a busy
          timeout (set via a ``connect`` event) so multiple Gunicorn workers /
          threads read and write one file safely.
        * Postgres/MySQL — a real connection pool with ``pool_pre_ping`` (heals
          connections dropped by the DB or a proxy) and ``pool_recycle`` (avoids
          stale connections after idle periods — common on Neon/managed PG).
    """
    global _engine
    if _engine is not None:
        return _engine

    url = _resolved_url()
    connect_args: dict = {}
    engine_kwargs: dict = {"pool_pre_ping": True, "future": True}

    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
    else:
        engine_kwargs.update(
            pool_size=int(getattr(config, "db_pool_size", 5) or 5),
            max_overflow=int(getattr(config, "db_max_overflow", 10) or 10),
            pool_recycle=int(getattr(config, "db_pool_recycle", 1800) or 1800),
            pool_timeout=int(getattr(config, "db_pool_timeout", 30) or 30),
        )

    _engine = create_engine(url, connect_args=connect_args, **engine_kwargs)

    if _engine.dialect.name == "sqlite":
        _install_sqlite_pragmas(_engine)

    logger.info(
        "DATABASE | Engine initialised | backend=%s driver=%s",
        _engine.dialect.name,
        _engine.driver,
    )
    return _engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Apply the concurrency-safety PRAGMAs the app historically relied on.

    Every new SQLite connection gets WAL journalling and a 30s busy timeout, so
    multiple workers/threads read and write the one file safely — exactly the
    behaviour the old per-module ``sqlite3.connect`` helpers provided.

    NOTE: ``PRAGMA foreign_keys`` is intentionally left at SQLite's default
    (OFF). The historical commerce engine did not enable FK enforcement, and
    some flows (e.g. marking a cart converted against an externally-created
    order id) rely on that. Enabling it here would be a behaviour change and
    could break existing paths, so we preserve the prior semantics. (PostgreSQL
    always enforces declared foreign keys — see the Migration Guide's note on
    this backend difference.)
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        try:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.close()
        except Exception as exc:  # noqa: BLE001 - never fail a checkout on PRAGMA
            logger.debug("DATABASE | sqlite PRAGMA setup skipped: %s", exc)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def get_session_factory() -> sessionmaker:
    """Lazily create (once) and return the one session factory (SessionLocal)."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commit on success, rollback on error, always close."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Backend introspection helpers (used by health probes, compat shim, migrations)
# --------------------------------------------------------------------------- #
def backend_name() -> str:
    """Return the active dialect name, e.g. ``'sqlite'`` or ``'postgresql'``."""
    return get_engine().dialect.name


def is_sqlite() -> bool:
    """True when the active backend is SQLite."""
    return backend_name() == "sqlite"


def is_postgres() -> bool:
    """True when the active backend is PostgreSQL."""
    return backend_name() in ("postgresql", "postgres")


# --------------------------------------------------------------------------- #
# Resilience: retry transient disconnects
# --------------------------------------------------------------------------- #
def run_with_retry(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 0.25) -> T:
    """Run ``fn`` with a short exponential backoff on *transient* DB errors.

    Only connection-level failures (``OperationalError`` /
    ``DBAPIError.connection_invalidated``) are retried — these are the stale/
    dropped-connection blips typical of managed Postgres (Neon idle scale-to-
    zero, proxy timeouts). Programming/integrity errors are re-raised at once so
    real bugs are never masked. ``pool_pre_ping`` already prevents most of
    these; this is the belt-and-suspenders layer for the rest.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (OperationalError, DBAPIError) as exc:
            transient = isinstance(exc, OperationalError) or getattr(
                exc, "connection_invalidated", False
            )
            if not transient or attempt == attempts:
                raise
            last_exc = exc
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "DATABASE | transient error (attempt %d/%d), retrying in %.2fs: %s",
                attempt, attempts, delay, exc,
            )
            time.sleep(delay)
    assert last_exc is not None  # pragma: no cover
    raise last_exc


# --------------------------------------------------------------------------- #
# Schema bootstrap
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Create every table that does not yet exist (idempotent, all backends).

    Imports :mod:`database.models` first so every ORM model — commerce, admin
    dashboard, OAuth token/state — is registered on ``Base.metadata`` before
    ``create_all`` runs.
    """
    from database import models  # noqa: F401 - registers all tables on Base

    Base.metadata.create_all(get_engine())
    logger.info("DATABASE | Schema ensured (create_all complete) on %s", backend_name())


def healthcheck() -> bool:
    """Return True if a trivial ``SELECT 1`` succeeds against the database."""

    def _probe() -> bool:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    return run_with_retry(_probe)


def dispose_engine() -> None:
    """Dispose the engine + session factory (test helper / graceful shutdown)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
