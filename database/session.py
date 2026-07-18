"""
database/session.py
--------------------
Canonical session accessors for the unified database layer.

Thin facade over :mod:`database.db` exposing the ONE session factory and the
transactional ``session_scope`` context manager under the conventional names
(``SessionLocal``, ``session_scope``). There is exactly one session factory in
the project, bound to the one engine.

    from database.session import SessionLocal, session_scope

    with session_scope() as session:
        ...  # commits on success, rolls back on error, always closes
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from database.db import get_session_factory, session_scope


def SessionLocal() -> Session:  # noqa: N802 - conventional factory name
    """Return a new ORM :class:`~sqlalchemy.orm.Session` from the one factory.

    Callers that manage their own commit/rollback can use this directly; most
    code should prefer :func:`session_scope` for automatic transaction handling.
    """
    return get_session_factory()()


__all__ = ["SessionLocal", "session_scope", "get_session_factory"]
