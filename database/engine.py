"""
database/engine.py
-------------------
Canonical engine accessors for the unified database layer.

This module is a thin, stable facade over :mod:`database.db` so the package
matches the requested ``database/{engine,session,models}.py`` structure while
keeping a single source of truth: there is exactly ONE engine in the whole
project, created lazily in :mod:`database.db`. Import the engine from here (or
from :mod:`database.db` — both return the same object).

    from database.engine import engine, get_engine, normalize_database_url
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from database.db import (  # re-export the one engine + helpers
    backend_name,
    dispose_engine,
    get_engine,
    is_postgres,
    is_sqlite,
    normalize_database_url,
    run_with_retry,
)


def _engine() -> Engine:
    """Return the single shared engine (lazily created on first use)."""
    return get_engine()


class _EngineProxy:
    """Attribute/callable proxy so ``engine`` resolves the lazy singleton.

    ``from database.engine import engine`` yields this proxy; attribute access
    (``engine.connect()``, ``engine.dialect``) is forwarded to the real engine,
    which is created on first touch. This avoids building the engine at import
    time (which would break the import-safe, opt-in database package contract).
    """

    def __getattr__(self, item: str):
        return getattr(_engine(), item)

    def __call__(self) -> Engine:
        return _engine()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<EngineProxy {_engine()!r}>"


engine = _EngineProxy()

__all__ = [
    "engine",
    "get_engine",
    "normalize_database_url",
    "backend_name",
    "is_sqlite",
    "is_postgres",
    "run_with_retry",
    "dispose_engine",
]
