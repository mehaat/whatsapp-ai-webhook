"""
database/compat.py
-------------------
A tiny, backward-compatible DBAPI shim over the ONE shared SQLAlchemy engine.

Purpose
=======
The Admin Dashboard was written against the stdlib ``sqlite3`` API: it opens a
connection, calls ``conn.execute("… ?", params).fetchone()`` and reads rows both
by column name (``row["id"]``) and by position (``row[0]``). That API — and the
``?`` placeholder style — is SQLite-specific and does **not** work on Postgres.

Rather than rewrite ~600 lines of finely-tuned dashboard SQL as ORM (high risk
of subtly changing chart/aggregation behaviour), this shim lets that exact SQL
run **unchanged** on whichever backend ``DATABASE_URL`` selects, by:

    1. Borrowing a pooled DBAPI connection from the ONE engine
       (``engine.raw_connection()``) — no ``sqlite3.connect()`` anywhere.
    2. Translating ``?`` placeholders to the driver's paramstyle
       (``%s`` for psycopg / pyformat) at execute time.
    3. Returning rows that support **both** ``row["col"]`` and ``row[i]``
       access (like ``sqlite3.Row``), regardless of backend.

The result: one engine, one pool, Postgres-compatible, with zero behaviour
change for the dashboard. Table creation is owned by the ORM models
(``create_all``); this shim is purely for the existing hand-written queries.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from database.db import get_engine
from utils.logging import logger

# Serialises SQLite writers within a process (SQLite allows one writer at a
# time). A no-op cost on Postgres, where the pool + MVCC handle concurrency, so
# it is only acquired when the active backend is SQLite.
_sqlite_write_lock = threading.Lock()


class Row(Sequence):
    """An immutable row supporting name access, index access and iteration.

    Mirrors the ergonomics of :class:`sqlite3.Row` so existing dashboard code
    (``row["id"]``, ``row[0]``, ``[r["shop"] for r in rows]``, ``dict(row)``)
    works identically on SQLite and Postgres.
    """

    __slots__ = ("_columns", "_values", "_index")

    def __init__(self, columns: Tuple[str, ...], values: Tuple[Any, ...]) -> None:
        self._columns = columns
        self._values = values
        self._index = {name: i for i, name in enumerate(columns)}

    def __getitem__(self, key):  # noqa: ANN001
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def keys(self) -> List[str]:
        """Column names, in select order (parity with ``sqlite3.Row.keys``)."""
        return list(self._columns)

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style ``.get`` by column name."""
        idx = self._index.get(key)
        return self._values[idx] if idx is not None else default

    def __contains__(self, key: object) -> bool:
        return key in self._index if isinstance(key, str) else key in self._values

    def _asdict(self) -> Dict[str, Any]:
        return {name: self._values[i] for i, name in enumerate(self._columns)}

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Row({self._asdict()!r})"


def _translate(sql: str, qmark_to_pyformat: bool) -> str:
    """Translate ``?`` placeholders to ``%s`` when the driver needs pyformat.

    Only applied for pyformat/format drivers (psycopg). Any literal ``%`` in the
    SQL text is first doubled to ``%%`` so it survives the driver's own
    percent-formatting. On SQLite the SQL is returned unchanged.
    """
    if not qmark_to_pyformat:
        return sql
    if "%" in sql:
        sql = sql.replace("%", "%%")
    return sql.replace("?", "%s")


class _Cursor:
    """Cursor wrapper: translates placeholders and returns :class:`Row` objects."""

    def __init__(self, raw_cursor, qmark_to_pyformat: bool) -> None:  # noqa: ANN001
        self._cur = raw_cursor
        self._pyformat = qmark_to_pyformat

    # -- execution -------------------------------------------------------- #
    def execute(self, sql: str, params: Sequence[Any] = ()) -> "_Cursor":
        self._cur.execute(_translate(sql, self._pyformat), tuple(params) if params else params)
        return self

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> "_Cursor":
        self._cur.executemany(_translate(sql, self._pyformat), seq_of_params)
        return self

    # -- fetching --------------------------------------------------------- #
    def _columns(self) -> Tuple[str, ...]:
        desc = self._cur.description
        return tuple(d[0] for d in desc) if desc else ()

    def fetchone(self) -> Optional[Row]:
        raw = self._cur.fetchone()
        return Row(self._columns(), tuple(raw)) if raw is not None else None

    def fetchall(self) -> List[Row]:
        cols = self._columns()
        return [Row(cols, tuple(r)) for r in self._cur.fetchall()]

    def fetchmany(self, size: Optional[int] = None) -> List[Row]:
        cols = self._columns()
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        return [Row(cols, tuple(r)) for r in rows]

    # -- attributes ------------------------------------------------------- #
    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):  # noqa: ANN201 - backend-dependent
        return getattr(self._cur, "lastrowid", None)

    @property
    def description(self):  # noqa: ANN201
        return self._cur.description

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:  # noqa: BLE001
            pass

    def __iter__(self) -> Iterator[Row]:
        cols = self._columns()
        for r in self._cur:
            yield Row(cols, tuple(r))


class PortableConnection:
    """A ``sqlite3``-style connection backed by the ONE shared engine's pool.

    Exposes the small slice of the stdlib DBAPI the dashboard actually uses —
    ``execute``/``executemany``/``executescript``/``cursor``/``commit``/
    ``rollback``/``close`` — while transparently translating placeholders and
    returning name+index rows on any backend.
    """

    def __init__(self) -> None:
        engine = get_engine()
        self._dialect = engine.dialect.name
        # psycopg (v2/v3) uses pyformat/format; sqlite uses qmark.
        self._pyformat = engine.dialect.paramstyle in ("pyformat", "format")
        self._raw = engine.raw_connection()

    # -- convenience: sqlite3-style connection.execute -------------------- #
    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        cur = self.cursor()
        return cur.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> _Cursor:
        cur = self.cursor()
        return cur.executemany(sql, seq_of_params)

    def executescript(self, script: str) -> None:
        """Execute a ``;``-separated batch of statements (schema helper).

        Table creation is normally handled by the ORM ``create_all``; this is
        kept only for parity and any ad-hoc multi-statement DDL. ``PRAGMA``
        statements are skipped on non-SQLite backends.
        """
        cur = self._raw.cursor()
        try:
            for statement in script.split(";"):
                stmt = statement.strip()
                if not stmt:
                    continue
                if not self._pyformat and stmt.upper().startswith("PRAGMA"):
                    cur.execute(stmt)
                    continue
                if self._pyformat and stmt.upper().startswith("PRAGMA"):
                    # Postgres has no PRAGMA — silently skip.
                    continue
                cur.execute(_translate(stmt, self._pyformat))
        finally:
            cur.close()

    def cursor(self) -> _Cursor:
        return _Cursor(self._raw.cursor(), self._pyformat)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        # Returns the pooled connection to the pool (does not hard-close it).
        try:
            self._raw.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("DBCOMPAT | connection close issue: %s", exc)


@contextmanager
def portable_conn(write: bool = False) -> Iterator[PortableConnection]:
    """Yield a portable connection, committing on success for writes.

    Args:
        write: When True, commit on clean exit and roll back on error. On SQLite
            writers are additionally serialised with a process-local lock to
            mirror the historical single-writer guarantee.
    """
    conn = PortableConnection()
    use_lock = write and conn._dialect == "sqlite"  # noqa: SLF001 - internal check
    if use_lock:
        _sqlite_write_lock.acquire()
    try:
        yield conn
        if write:
            conn.commit()
        else:
            # End any implicit read transaction so PG connections don't linger
            # "idle in transaction" when returned to the pool.
            conn.rollback()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()
        if use_lock:
            _sqlite_write_lock.release()


__all__ = ["Row", "PortableConnection", "portable_conn"]
