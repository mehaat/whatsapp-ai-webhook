"""
database/migrate_v10_1.py
--------------------------
v10.1 database unification migration.

Before v10.1 the admin dashboard stored its data in a SEPARATE ``mehaat_admin.db``
file. v10.1 unifies everything into one ``mehaat.db``; the admin dashboard's
three tables that collide with the commerce schema (``customers``,
``conversations``, ``orders``) are renamed to ``dash_customers`` /
``dash_conversations`` / ``dash_orders``.

This migration copies the legacy ``mehaat_admin.db`` rows into the unified
database (into the ``dash_*`` names for the renamed tables, same names for the
rest) using ``INSERT OR IGNORE`` so it is idempotent and never overwrites or
deletes existing data. When done it renames the legacy file to
``*.migrated-v10_1`` so it is not reprocessed.

Safe to run on every boot: if no legacy file exists (fresh installs, or already
migrated) it is a no-op.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Dict, List, Optional

from utils.logging import logger

# Legacy admin table -> unified table name.
_TABLE_MAP: Dict[str, str] = {
    "users": "users",
    "customers": "dash_customers",
    "conversations": "dash_conversations",
    "messages": "messages",
    "ai_history": "ai_history",
    "products": "products",
    "product_sends": "product_sends",
    "orders": "dash_orders",
}


def _legacy_admin_db_paths(unified_path: str) -> List[str]:
    """Candidate locations of a pre-v10.1 ``mehaat_admin.db`` (excluding unified)."""
    candidates: List[str] = []
    explicit = os.environ.get("ADMIN_DB_PATH", "").strip()
    if explicit:
        candidates.append(explicit)
    token_store = os.environ.get("TOKEN_STORE_PATH", "").strip()
    if token_store:
        candidates.append(os.path.join(os.path.dirname(token_store) or ".", "mehaat_admin.db"))
    candidates.append(os.path.join(os.path.dirname(unified_path) or ".", "mehaat_admin.db"))
    candidates.append(os.path.join(os.getcwd(), "mehaat_admin.db"))
    # De-dup, keep existing files that are NOT the unified db.
    seen, out = set(), []
    for path in candidates:
        ap = os.path.abspath(path)
        if ap in seen or ap == os.path.abspath(unified_path):
            continue
        seen.add(ap)
        if os.path.isfile(ap):
            out.append(ap)
    return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _copy_table(conn: sqlite3.Connection, src: str, dst: str) -> int:
    """Copy rows from attached ``legacy.src`` into ``dst`` by shared columns."""
    if not _table_exists(conn, dst):
        # The unified schema (admin init_db) should have created it already.
        logger.warning("MIGRATE_v10_1 | target table %s missing; skipping %s", dst, src)
        return 0
    src_cols = {r[1] for r in conn.execute(f'PRAGMA legacy.table_info("{src}")')}
    dst_cols = _columns(conn, dst)
    shared = [c for c in dst_cols if c in src_cols]
    if not shared:
        return 0
    col_list = ", ".join(f'"{c}"' for c in shared)
    before = conn.execute(f'SELECT COUNT(*) FROM "{dst}"').fetchone()[0]
    conn.execute(
        f'INSERT OR IGNORE INTO "{dst}" ({col_list}) '
        f'SELECT {col_list} FROM legacy."{src}"'
    )
    after = conn.execute(f'SELECT COUNT(*) FROM "{dst}"').fetchone()[0]
    return after - before


def merge_admin_db(unified_path: Optional[str] = None) -> dict:
    """Merge a legacy ``mehaat_admin.db`` into the unified database.

    Idempotent and safe: no-op when no legacy file exists. Returns a report.

    Scope: this is a legacy, **SQLite-only** one-time recovery step that merges a
    pre-v10.1 ``mehaat_admin.db`` into the unified SQLite file via ``ATTACH``.
    When the active backend is PostgreSQL (Neon/Render) there is no legacy
    SQLite admin file to merge, so this returns an immediate no-op and never
    opens a ``sqlite3`` connection.
    """
    from utils.dbpath import canonical_sqlite_path

    report = {"migrated": False, "source": None, "copied": {}, "renamed_to": None}

    # Only meaningful on SQLite; skip entirely on server backends.
    try:
        from database.db import is_sqlite

        if not is_sqlite():
            logger.info("MIGRATE_v10_1 | Non-SQLite backend; legacy admin merge skipped")
            return report
    except Exception as exc:  # noqa: BLE001 - be conservative, but don't crash
        logger.debug("MIGRATE_v10_1 | backend check failed (%s); assuming SQLite", exc)

    unified_path = unified_path or canonical_sqlite_path()

    legacy_paths = _legacy_admin_db_paths(unified_path)
    if not legacy_paths:
        return report

    legacy = legacy_paths[0]
    logger.info("MIGRATE_v10_1 | Found legacy admin DB at %s; merging into %s",
                legacy, unified_path)
    conn = None
    try:
        # Ensure the unified admin schema (dash_* tables) exists first.
        try:
            from admin.db import init_db

            init_db()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MIGRATE_v10_1 | admin schema init warning: %s", exc)

        conn = sqlite3.connect(unified_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("ATTACH DATABASE ? AS legacy", (legacy,))
        conn.execute("BEGIN IMMEDIATE;")
        copied = {}
        for src, dst in _TABLE_MAP.items():
            if _table_exists_legacy(conn, src):
                n = _copy_table(conn, src, dst)
                if n:
                    copied[f"{src}->{dst}"] = n
        conn.commit()
        conn.execute("DETACH DATABASE legacy")
        report["migrated"] = True
        report["source"] = legacy
        report["copied"] = copied

        # Rename the legacy file so it is not reprocessed.
        target = legacy + ".migrated-v10_1"
        try:
            if os.path.exists(target):
                target = legacy + f".migrated-v10_1.{int(os.path.getmtime(legacy))}"
            os.rename(legacy, target)
            report["renamed_to"] = target
        except Exception as exc:  # noqa: BLE001
            logger.warning("MIGRATE_v10_1 | could not rename legacy file: %s", exc)

        logger.info("MIGRATE_v10_1 | Done. Copied=%s legacy_renamed_to=%s",
                    copied, report["renamed_to"])
    except Exception as exc:  # noqa: BLE001 - migration must never crash startup
        logger.error("MIGRATE_v10_1 | Migration failed (continuing): %s", exc)
        try:
            if conn is not None:
                conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if conn is not None:
            conn.close()
    return report


def _table_exists_legacy(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM legacy.sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


if __name__ == "__main__":  # pragma: no cover - manual migration entrypoint
    print(merge_admin_db())
