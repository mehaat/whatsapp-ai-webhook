"""
commerce/audit_chain.py
------------------------
v8.0 tamper-evident audit hash chain.

Every :class:`~database.models.AuditLog` row carries two extra columns:
``prev_hash`` (the ``row_hash`` of the previous chained row) and ``row_hash``
(a SHA-256 over this row's content plus ``prev_hash``). Chaining the hashes
makes the audit trail tamper-evident: altering, inserting or deleting any row
breaks every downstream hash, which :func:`verify_chain` detects.

The hash deliberately excludes ``created_at`` so it can be computed *before* the
row is flushed (SQLAlchemy assigns server/default timestamps at flush time,
which would otherwise make the hash unstable).

All functions are defensive: auditing must never break the operation it records,
so nothing here raises to the caller.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from utils.logging import logger


def compute_row_hash(
    prev_hash: Optional[str],
    actor,
    action,
    entity,
    entity_id,
    detail,
) -> str:
    """Return the SHA-256 hex digest for one audit row.

    The digest is taken over a canonical, pipe-delimited string of the row's
    logical content plus the previous row's hash::

        f"{prev_hash or ''}|{actor}|{action}|{entity or ''}|{entity_id or ''}|{detail or ''}"

    ``created_at`` is intentionally excluded (see the module docstring).

    Args:
        prev_hash: The previous chained row's ``row_hash`` (or ``None``).
        actor: The acting user/system.
        action: The dotted action name.
        entity: The affected entity type (or ``None``).
        entity_id: The affected entity id (or ``None``).
        detail: Free-text detail (or ``None``).

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    canonical = (
        f"{prev_hash or ''}|{actor}|{action}|"
        f"{entity or ''}|{entity_id or ''}|{detail or ''}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def last_hash(session) -> Optional[str]:
    """Return the ``row_hash`` of the most recent chained audit row.

    "Most recent" is by ``id`` descending, restricted to rows that already have
    a non-null ``row_hash`` (pre-v8 legacy rows carry ``NULL`` and are ignored).

    Args:
        session: An open SQLAlchemy session.

    Returns:
        The latest ``row_hash``, or ``None`` when the chain is empty.
    """
    try:
        from database.models import AuditLog

        row = (
            session.query(AuditLog)
            .filter(AuditLog.row_hash.isnot(None))
            .order_by(AuditLog.id.desc())
            .first()
        )
        return row.row_hash if row is not None else None
    except Exception as exc:  # noqa: BLE001 - chaining must never break auditing
        logger.debug("AUDIT | last_hash failed: %s", exc)
        return None


def apply_chain(session, audit_obj) -> None:
    """Stamp ``prev_hash`` + ``row_hash`` onto a new audit row.

    Intended to be called from ``OrderService._audit`` after the row's logical
    fields (actor/action/entity/entity_id/detail) are set. The session is
    flushed first so any earlier pending rows in the same transaction are
    visible to :func:`last_hash` (giving each row a distinct predecessor); the
    row's own ``id``/``created_at`` are never relied upon.

    Never raises.

    Args:
        session: The open session the row lives in.
        audit_obj: The :class:`~database.models.AuditLog` instance to stamp.
    """
    try:
        # Persist earlier pending rows so their hashes chain correctly. This is
        # safe even when audit_obj itself is already in the session (it simply
        # gets an id and a still-NULL row_hash, which last_hash skips).
        try:
            session.flush()
        except Exception as exc:  # noqa: BLE001
            logger.debug("AUDIT | pre-chain flush failed: %s", exc)

        prev = last_hash(session)
        audit_obj.prev_hash = prev
        audit_obj.row_hash = compute_row_hash(
            prev,
            audit_obj.actor,
            audit_obj.action,
            audit_obj.entity,
            audit_obj.entity_id,
            audit_obj.detail,
        )
    except Exception as exc:  # noqa: BLE001 - never break the audited operation
        logger.debug("AUDIT | apply_chain failed: %s", exc)


def verify_chain(limit: Optional[int] = None) -> dict:
    """Recompute and verify the whole audit hash chain.

    Walks every :class:`~database.models.AuditLog` row in ``id`` ascending
    order, recomputing each ``row_hash`` from the running predecessor. Legacy
    rows with a ``NULL`` ``row_hash`` are skipped and treated as a chain start
    (they do not advance the running predecessor).

    Never raises.

    Args:
        limit: Optional cap on the number of rows to walk (from the start).

    Returns:
        ``{"ok": bool, "count": int, "broken_at": id_or_None}`` where ``count``
        is the number of chained (non-null) rows verified and ``broken_at`` is
        the id of the first row whose stored hash does not match.
    """
    try:
        from database.db import session_scope
        from database.models import AuditLog

        count = 0
        running: Optional[str] = None
        with session_scope() as session:
            q = session.query(AuditLog).order_by(AuditLog.id.asc())
            if limit:
                q = q.limit(limit)
            for row in q.all():
                if row.row_hash is None:
                    # Pre-v8 legacy row: not part of the chain.
                    continue
                expected = compute_row_hash(
                    running,
                    row.actor,
                    row.action,
                    row.entity,
                    row.entity_id,
                    row.detail,
                )
                if expected != row.row_hash:
                    return {"ok": False, "count": count, "broken_at": row.id}
                count += 1
                running = row.row_hash
        return {"ok": True, "count": count, "broken_at": None}
    except Exception as exc:  # noqa: BLE001 - verification must never crash callers
        logger.error("AUDIT | verify_chain failed: %s", exc)
        return {"ok": False, "count": 0, "broken_at": None}
