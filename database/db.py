"""
database/db.py
---------------
SQLAlchemy engine + session management for ME-HAAT Fashion AI Bot v4.0.

Defaults to SQLite (``sqlite:///mehaat.db``) and works unchanged against
PostgreSQL by setting ``DATABASE_URL`` (e.g. Render's managed Postgres URL).
MySQL is also supported by SQLAlchemy with the appropriate driver.

This module imports SQLAlchemy at module load; the ``database`` package
guards that import so the wider app never fails if SQLAlchemy is absent or
``USE_DATABASE`` is off.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import config
from utils.logging import logger


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine = None
_SessionFactory = None


def normalize_database_url(url: str) -> str:
    """Normalize a DATABASE_URL for SQLAlchemy 2.0.

    Render/Heroku hand out ``postgres://`` URLs, which SQLAlchemy 2.0 rejects
    with ``NoSuchModuleError``. Rewrite the scheme to the ``postgresql+psycopg2``
    dialect so a managed-Postgres deployment works out of the box.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def get_engine():
    """Lazily create (once) and return the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = normalize_database_url(config.database_url)
        connect_args = {}
        engine_kwargs = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            # v10.1: pin sqlite to the ONE canonical absolute file so the
            # commerce data lives in the same mehaat.db as the token store and
            # admin dashboard (fixes DB-path fragmentation).
            try:
                from utils.dbpath import canonical_sqlite_url

                url = canonical_sqlite_url()
            except Exception as exc:  # noqa: BLE001 - fall back to the raw URL
                logger.warning("DATABASE | canonical path unavailable: %s", exc)
            connect_args = {"check_same_thread": False}
        else:
            # Sensible pool defaults for server databases (Postgres/MySQL).
            # pool_recycle guards against stale connections dropped by the DB or
            # a proxy after idle periods (common on managed Postgres).
            engine_kwargs.update(
                pool_size=5,
                max_overflow=10,
                pool_recycle=1800,
                pool_timeout=30,
            )
        _engine = create_engine(url, connect_args=connect_args, **engine_kwargs)
        logger.info("DATABASE | Engine initialised for %s", url.split("://", 1)[0])
    return _engine


def get_session_factory():
    """Lazily create (once) and return the session factory."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional session scope with commit/rollback handling."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables that do not yet exist (idempotent)."""
    # Import models so their tables are registered on Base.metadata.
    from database import models  # noqa: F401

    Base.metadata.create_all(get_engine())
    logger.info("DATABASE | Schema ensured (create_all complete)")


def healthcheck() -> bool:
    """Return True if a trivial query succeeds against the database."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
