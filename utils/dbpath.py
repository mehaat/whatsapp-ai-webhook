"""
utils/dbpath.py
----------------
v10.1: the single source of truth for the unified SQLite database path.

Historically three subsystems each resolved their own SQLite file:
    - shopify/auth.py  (OAuth token store)  -> "first writable candidate wins"
    - admin/db.py      (dashboard data)      -> mehaat_admin.db
    - database/db.py   (SQLAlchemy commerce) -> DATABASE_URL

That fragmentation caused the production OAuth bug: a *relative* sqlite URL
resolved to an **ephemeral** working-directory file that was wiped on restart
(``installed_shops=1`` at callback, ``shop_count=0`` after redeploy), and the
three layers could disagree about where "the database" was.

This module resolves ONE canonical, **absolute**, deterministic path (no
"first writable wins" guessing) that every layer uses, so tokens, admin data
and commerce data all live in a single ``mehaat.db``.

Resolution priority (first match wins, evaluated once and cached):
    1. ``UNIFIED_DB_PATH`` env override (explicit operator control).
    2. A sqlite path parsed from ``DATABASE_URL``.
    3. A ``mehaat.db`` next to ``TOKEN_STORE_PATH`` (Render's mounted disk).
    4. ``/var/data/mehaat.db`` when ``/var/data`` exists (Render default).
    5. ``mehaat.db`` in the current working directory (dev/tests).

The result is always made absolute, so a changing CWD can never repoint the
database. When ``DATABASE_URL`` is a non-sqlite backend (e.g. PostgreSQL) the
commerce layer uses that backend directly; this canonical sqlite file is then
used only by the token store + admin dashboard (which remain sqlite).
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from utils.logging import logger

_CANONICAL: Optional[str] = None
_LOCK = threading.Lock()


def sqlite_path_from_url(url: Optional[str]) -> Optional[str]:
    """Parse a filesystem path from a ``sqlite://`` URL, honouring slash count.

    ``sqlite:///rel.db``  -> ``rel.db``          (relative, 3 slashes)
    ``sqlite:////abs.db`` -> ``/abs.db``         (absolute, 4 slashes)
    Returns None for non-sqlite URLs or ``:memory:``.
    """
    if not url:
        return None
    url = url.strip()
    if not url.startswith("sqlite"):
        return None
    # Drop the scheme; SQLAlchemy also supports sqlite+pysqlite://.
    try:
        rest = url.split("://", 1)[1]
    except IndexError:
        return None
    if not rest or ":memory:" in rest:
        return None
    # rest is like "/rel.db" (relative) or "//abs.db" (absolute).
    if rest.startswith("//"):
        return rest[1:]  # absolute
    return rest.lstrip("/")  # relative


def canonical_sqlite_path() -> str:
    """Return the one absolute canonical SQLite path (resolved once, cached)."""
    global _CANONICAL
    if _CANONICAL is not None:
        return _CANONICAL
    with _LOCK:
        if _CANONICAL is not None:
            return _CANONICAL

        override = os.environ.get("UNIFIED_DB_PATH", "").strip()
        from_url = sqlite_path_from_url(os.environ.get("DATABASE_URL", ""))
        token_store = os.environ.get("TOKEN_STORE_PATH", "").strip()

        if override:
            path = override
        elif from_url:
            path = from_url
        elif token_store:
            path = os.path.join(os.path.dirname(token_store) or ".", "mehaat.db")
        elif os.path.isdir("/var/data"):
            path = "/var/data/mehaat.db"
        else:
            path = "mehaat.db"

        # KEY FIX: always absolute so a changing CWD cannot repoint the DB.
        path = os.path.abspath(path)
        directory = os.path.dirname(path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.error("DBPATH | Could not create %s: %s", directory, exc)

        _CANONICAL = path
        logger.info("DBPATH | Canonical unified SQLite database: %s", path)
        return _CANONICAL


from pathlib import Path

def canonical_sqlite_url() -> str:
    """
    Return a SQLAlchemy-compatible SQLite URL.
    """
    db = Path(canonical_sqlite_path()).resolve()

    if os.name == "nt":
        return "sqlite:///" + db.as_posix()

   return "sqlite:////" + db.as_posix().lstrip("/")


def database_is_sqlite() -> bool:
    """True when the configured DATABASE_URL is sqlite (or unset -> sqlite)."""
    url = os.environ.get("DATABASE_URL", "").strip()
    return (not url) or url.startswith("sqlite")


def database_size_bytes() -> int:
    """Return the canonical database file size in bytes (0 if absent)."""
    try:
        return os.path.getsize(canonical_sqlite_path())
    except OSError:
        return 0


def reset_cache_for_tests() -> None:
    """Clear the cached path (test helper only)."""
    global _CANONICAL
    with _LOCK:
        _CANONICAL = None
