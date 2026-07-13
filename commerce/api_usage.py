"""
commerce/api_usage.py
----------------------
Per-key API **usage recording and reporting** for the ME-HAAT Fashion AI Bot
v9.0 developer portal.

Usage is rolled up per key prefix per UTC day into the ``api_usage`` table
(see :class:`database.models.ApiUsage`), keyed uniquely by ``(prefix, day)``.
Recording is an idempotent upsert: the first request for a key on a given day
inserts the row, subsequent requests increment its ``count`` and refresh
``last_endpoint``.

Design guarantees:
    * :func:`record_usage` is called on the hot request path, so it **never
      raises** — any failure is logged at debug level and swallowed. Metering
      must never break the API.
    * The reporting helpers (:func:`usage_for`, :func:`usage_summary`) also
      degrade to safe empty values rather than raising.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from database.db import session_scope
from database.models import ApiKey, ApiUsage
from utils.logging import logger


def _today() -> str:
    """Return today's date as a ``YYYY-MM-DD`` string in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _window_start(days: int) -> str:
    """Return the ``YYYY-MM-DD`` lower bound for a trailing ``days`` window."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    if days < 1:
        days = 1
    start = datetime.now(timezone.utc) - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Recording (hot path — never raises)
# --------------------------------------------------------------------------

def record_usage(prefix: str, endpoint: str) -> None:
    """Upsert today's usage row for ``prefix``, incrementing its count.

    Called from the API auth layer on every authenticated key request. Any
    failure (bad prefix, DB hiccup, race on the unique constraint) is logged
    and swallowed — metering must never break the request.

    Args:
        prefix: The API key's public prefix (``ApiKey.prefix``).
        endpoint: A short label for the endpoint hit (stored as
            ``last_endpoint``, truncated to fit the column).
    """
    if not prefix:
        return
    try:
        prefix = str(prefix)[:16]
        endpoint = (str(endpoint) if endpoint is not None else "")[:128]
        day = _today()
        with session_scope() as db:
            row = (
                db.query(ApiUsage)
                .filter(ApiUsage.prefix == prefix, ApiUsage.day == day)
                .first()
            )
            if row is None:
                db.add(ApiUsage(
                    prefix=prefix, day=day, count=1, last_endpoint=endpoint,
                ))
            else:
                row.count = (row.count or 0) + 1
                row.last_endpoint = endpoint
    except Exception as exc:  # noqa: BLE001 - metering must never break the API
        # A rare race on uq_api_usage_day (concurrent first insert) lands here;
        # a retry as a plain increment keeps the count correct.
        logger.debug("APIUSAGE | record_usage(%r) first attempt failed: %s", prefix, exc)
        try:
            with session_scope() as db:
                row = (
                    db.query(ApiUsage)
                    .filter(ApiUsage.prefix == prefix, ApiUsage.day == _today())
                    .first()
                )
                if row is not None:
                    row.count = (row.count or 0) + 1
                    row.last_endpoint = endpoint
        except Exception as exc2:  # noqa: BLE001
            logger.debug("APIUSAGE | record_usage(%r) retry failed: %s", prefix, exc2)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def usage_for(prefix: str, days: int = 30) -> Dict[str, Any]:
    """Return a per-day usage breakdown for a single key over a window.

    Args:
        prefix: The key's public prefix.
        days: Size of the trailing window in days (default 30).

    Returns:
        ``{"prefix", "total", "daily": [{"day", "count"}...], "last_endpoint"}``.
        On any failure returns a zeroed structure (never raises).
    """
    empty = {"prefix": prefix, "total": 0, "daily": [], "last_endpoint": None}
    if not prefix:
        return empty
    try:
        start = _window_start(days)
        with session_scope() as db:
            rows = (
                db.query(ApiUsage)
                .filter(ApiUsage.prefix == prefix, ApiUsage.day >= start)
                .order_by(ApiUsage.day.asc())
                .all()
            )
            daily = [{"day": r.day, "count": int(r.count or 0)} for r in rows]
            total = sum(d["count"] for d in daily)
            # last_endpoint from the most recent day present.
            last_endpoint = rows[-1].last_endpoint if rows else None
            return {
                "prefix": prefix,
                "total": total,
                "daily": daily,
                "last_endpoint": last_endpoint,
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("APIUSAGE | usage_for(%r) failed: %s", prefix, exc)
        return empty


def usage_summary(days: int = 30) -> List[Dict[str, Any]]:
    """Return per-key usage totals over a window, joined with the key name.

    Aggregates every ``api_usage`` row in the trailing window by prefix, joins
    each prefix's :class:`ApiKey` metadata (name, when known), and returns the
    keys sorted by total usage descending.

    Args:
        days: Size of the trailing window in days (default 30).

    Returns:
        A list of ``{"prefix", "name", "total", "last_endpoint", "last_day"}``
        dicts, busiest first. Empty on any failure (never raises).
    """
    try:
        start = _window_start(days)
        with session_scope() as db:
            rows = (
                db.query(ApiUsage)
                .filter(ApiUsage.day >= start)
                .all()
            )
            # Map prefix -> ApiKey.name for labelling (best-effort).
            names: Dict[str, str] = {}
            try:
                for k in db.query(ApiKey).all():
                    names[k.prefix] = k.name
            except Exception as exc:  # noqa: BLE001 - name join is best-effort
                logger.debug("APIUSAGE | key-name join failed: %s", exc)

            agg: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                bucket = agg.setdefault(
                    r.prefix,
                    {"prefix": r.prefix, "name": names.get(r.prefix),
                     "total": 0, "last_endpoint": None, "last_day": None},
                )
                bucket["total"] += int(r.count or 0)
                # Track the most recent day / endpoint seen.
                if bucket["last_day"] is None or r.day >= bucket["last_day"]:
                    bucket["last_day"] = r.day
                    bucket["last_endpoint"] = r.last_endpoint

            return sorted(agg.values(), key=lambda d: d["total"], reverse=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("APIUSAGE | usage_summary failed: %s", exc)
        return []
