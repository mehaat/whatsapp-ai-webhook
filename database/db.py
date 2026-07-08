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


def get_engine():
    """Lazily create (once) and return the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = config.database_url
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            connect_args=connect_args,
        )
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
