"""
commerce/numbering.py
----------------------
Sequential, human-facing order-number generation, e.g. ``MH-2026-000001``.

Numbers are minted from a per-year row in the ``counters`` table inside a
transaction, so concurrent orders never collide. On PostgreSQL the row is
locked with ``SELECT ... FOR UPDATE``; SQLite serializes writers anyway, so the
same code path is correct on both backends.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config import config


def _year() -> int:
    return datetime.now(timezone.utc).year


def next_order_number(session) -> str:
    """Return the next order number and advance the counter within ``session``.

    Must be called inside an open transaction/session; the caller commits.
    """
    from database.models import Counter

    year = _year()
    key = f"order:{year}"

    row = session.get(Counter, key, with_for_update=True) if _supports_for_update(session) \
        else session.get(Counter, key)
    if row is None:
        row = Counter(name=key, value=0)
        session.add(row)
        session.flush()
    row.value = (row.value or 0) + 1
    session.flush()

    prefix = config.order_number_prefix or "MH"
    return f"{prefix}-{year}-{row.value:06d}"


def _supports_for_update(session) -> bool:
    """True when the bound engine supports row-level locking (Postgres)."""
    try:
        return session.bind.dialect.name not in {"sqlite"}
    except Exception:  # noqa: BLE001
        return False


def next_invoice_number(session) -> str:
    """Return the next invoice number, e.g. ``INV-2026-000001``."""
    from database.models import Counter

    year = _year()
    key = f"invoice:{year}"
    row = session.get(Counter, key)
    if row is None:
        row = Counter(name=key, value=0)
        session.add(row)
        session.flush()
    row.value = (row.value or 0) + 1
    session.flush()
    return f"INV-{year}-{row.value:06d}"
