#!/usr/bin/env python3
"""
scripts/migrate_sqlite_to_postgres.py
=====================================
One-off, **idempotent** data-copy tool: move existing rows from a local SQLite
``mehaat.db`` into a PostgreSQL database (e.g. Neon) so you don't lose current
production data when switching backends.

What it does
------------
1. Ensures the full schema exists on the **target** (Postgres) via the app's
   ORM metadata (``Base.metadata.create_all``) — so you can run this before or
   after ``alembic upgrade head`` either way.
2. For every table the app knows about (in FK-safe order), copies rows from the
   source SQLite file into the target, **skipping rows whose primary key already
   exists** on the target. Re-running is safe: already-copied rows are not
   duplicated.
3. Resets Postgres identity sequences (``SERIAL``) to ``MAX(id)`` afterwards, so
   subsequent inserts don't collide with the copied primary keys.

It only ever **reads** from SQLite and **writes** to Postgres — the source file
is never modified.

Usage
-----
    # Uses the canonical sqlite path as source and $DATABASE_URL as target:
    export DATABASE_URL='postgresql://user:pass@host/db?sslmode=require'
    python scripts/migrate_sqlite_to_postgres.py

    # Or specify explicitly:
    python scripts/migrate_sqlite_to_postgres.py \
        --source sqlite:////var/data/mehaat.db \
        --target 'postgresql://user:pass@host/db?sslmode=require'

    # Preview only (no writes):
    python scripts/migrate_sqlite_to_postgres.py --dry-run

Options
-------
    --source URL   Source SQLite URL or path (default: canonical sqlite path).
    --target URL   Target Postgres URL (default: $DATABASE_URL).
    --batch N      Insert batch size (default: 500).
    --only T[,T]   Restrict to a comma-separated list of table names.
    --skip T[,T]   Skip a comma-separated list of table names.
    --no-create    Do not run create_all on the target (assume schema exists).
    --dry-run      Report what would be copied without writing.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

# Make the project importable when run as a script from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import MetaData, Table, create_engine, func, insert, inspect, select
from sqlalchemy.engine import Engine


def _normalize_source(url_or_path: str) -> str:
    """Accept either a filesystem path or a ``sqlite://`` URL for the source."""
    if "://" in url_or_path:
        return url_or_path
    abspath = os.path.abspath(url_or_path)
    return "sqlite:////" + abspath.lstrip("/")


def _normalize_target(url: str) -> str:
    """Normalise the Postgres target URL to the psycopg (v3) dialect."""
    from database.db import normalize_database_url

    norm = normalize_database_url(url)
    if not norm.startswith("postgresql"):
        raise SystemExit(
            f"Target must be a PostgreSQL URL; got: {url!r}. "
            "This tool copies SQLite -> Postgres."
        )
    return norm


def _pk_columns(table: Table) -> List[str]:
    return [c.name for c in table.primary_key.columns]


def _existing_pks(engine: Engine, table: Table, pk_cols: List[str]) -> Set[Tuple]:
    """Return the set of primary-key tuples already present on the target."""
    if not pk_cols:
        return set()
    cols = [table.c[name] for name in pk_cols]
    with engine.connect() as conn:
        return {tuple(row) for row in conn.execute(select(*cols))}


def _copy_table(
    src_engine: Engine,
    tgt_engine: Engine,
    src_table: Table,
    tgt_table: Table,
    batch: int,
    dry_run: bool,
) -> Dict[str, int]:
    """Copy rows for one table; skip PKs already present on the target."""
    # Columns to copy = target columns that also exist in the source.
    src_col_names = set(src_table.c.keys())
    common_cols = [c.name for c in tgt_table.columns if c.name in src_col_names]
    if not common_cols:
        return {"read": 0, "inserted": 0, "skipped": 0}

    pk_cols = _pk_columns(tgt_table)
    existing = _existing_pks(tgt_engine, tgt_table, pk_cols) if not dry_run else set()

    read = inserted = skipped = 0
    pending: List[dict] = []

    def _flush() -> None:
        nonlocal inserted, pending
        if pending and not dry_run:
            with tgt_engine.begin() as conn:
                conn.execute(insert(tgt_table), pending)
        inserted += len(pending)
        pending = []

    sel = select(*[src_table.c[name] for name in common_cols])
    with src_engine.connect() as sconn:
        result = sconn.execution_options(stream_results=True).execute(sel)
        for row in result:
            read += 1
            mapping = dict(zip(common_cols, row))
            if pk_cols:
                key = tuple(mapping.get(pc) for pc in pk_cols)
                if key in existing:
                    skipped += 1
                    continue
                existing.add(key)
            pending.append(mapping)
            if len(pending) >= batch:
                _flush()
        _flush()

    return {"read": read, "inserted": inserted, "skipped": skipped}


def _reset_sequences(tgt_engine: Engine, tables: Sequence[Table]) -> None:
    """Advance Postgres SERIAL sequences to MAX(id) after copying explicit ids."""
    for table in tables:
        for col in table.primary_key.columns:
            # Only integer autoincrement-style PKs have an owned sequence.
            if not str(col.type).upper().startswith(("INTEGER", "BIGINT", "SMALLINT")):
                continue
            try:
                with tgt_engine.begin() as conn:
                    seq = conn.exec_driver_sql(
                        "SELECT pg_get_serial_sequence(%s, %s)",
                        (table.name, col.name),
                    ).scalar()
                    if not seq:
                        continue
                    max_id = conn.execute(select(func.max(col))).scalar()
                    if max_id is not None:
                        conn.exec_driver_sql(
                            "SELECT setval(%s, %s, true)", (seq, int(max_id))
                        )
            except Exception as exc:  # noqa: BLE001 - non-fatal housekeeping
                print(f"  ! sequence reset skipped for {table.name}.{col.name}: {exc}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Copy data from SQLite to PostgreSQL.")
    parser.add_argument("--source", default=None, help="Source SQLite URL or path.")
    parser.add_argument("--target", default=None, help="Target Postgres URL (default $DATABASE_URL).")
    parser.add_argument("--batch", type=int, default=500, help="Insert batch size.")
    parser.add_argument("--only", default="", help="Comma-separated tables to include.")
    parser.add_argument("--skip", default="", help="Comma-separated tables to skip.")
    parser.add_argument("--no-create", action="store_true", help="Do not create schema on target.")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not write.")
    args = parser.parse_args(argv)

    # Resolve source.
    if args.source:
        source_url = _normalize_source(args.source)
    else:
        from utils.dbpath import canonical_sqlite_path

        source_url = _normalize_source(canonical_sqlite_path())

    # Resolve target.
    target_raw = args.target or os.environ.get("DATABASE_URL", "")
    if not target_raw:
        raise SystemExit("No target: pass --target or set DATABASE_URL to a Postgres URL.")
    target_url = _normalize_target(target_raw)

    print(f"Source (read-only): {source_url}")
    print(f"Target            : {target_url.split('@')[-1] if '@' in target_url else target_url}")
    if args.dry_run:
        print("Mode              : DRY RUN (no writes)")

    # The app's ORM metadata is the canonical set of tables (FK-safe order).
    from database.db import Base
    from database import models  # noqa: F401 - registers every table on Base

    src_engine = create_engine(source_url)
    tgt_engine = create_engine(target_url, pool_pre_ping=True)

    # Ensure the schema exists on the target.
    if not args.no_create and not args.dry_run:
        Base.metadata.create_all(tgt_engine)
        print("Target schema ensured (create_all).")

    # Which tables actually exist in the source file?
    src_meta = MetaData()
    src_meta.reflect(bind=src_engine)
    source_tables = set(src_meta.tables.keys())

    only = {t.strip() for t in args.only.split(",") if t.strip()}
    skip = {t.strip() for t in args.skip.split(",") if t.strip()}

    totals = {"read": 0, "inserted": 0, "skipped": 0}
    copied_tables: List[Table] = []

    for table in Base.metadata.sorted_tables:  # FK-safe (parents first)
        name = table.name
        if only and name not in only:
            continue
        if name in skip:
            continue
        if name not in source_tables:
            continue  # nothing to copy from source
        src_table = src_meta.tables[name]
        stats = _copy_table(src_engine, tgt_engine, src_table, table, args.batch, args.dry_run)
        if stats["read"]:
            verb = "would copy" if args.dry_run else "copied"
            print(
                f"  {name:<28} read={stats['read']:<7} "
                f"{verb}={stats['inserted']:<7} skipped(existing)={stats['skipped']}"
            )
            copied_tables.append(table)
        for k in totals:
            totals[k] += stats[k]

    # Advance identity sequences so future inserts don't collide.
    if not args.dry_run and copied_tables and tgt_engine.dialect.name == "postgresql":
        print("Resetting identity sequences on target ...")
        _reset_sequences(tgt_engine, copied_tables)

    print(
        f"\nDone. tables={len(copied_tables)} rows_read={totals['read']} "
        f"inserted={totals['inserted']} skipped_existing={totals['skipped']}"
    )
    src_engine.dispose()
    tgt_engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
