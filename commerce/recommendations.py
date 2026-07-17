"""
commerce/recommendations.py
----------------------------
ME-HAAT Fashion AI Bot v9.0 — the recommendation engine.

Pure, read-only analytics over the existing commerce tables (``orders`` and
``order_items``). Everything here is expressed in plain SQLAlchemy so it works
identically on SQLite (dev/tests) and PostgreSQL (production), and every public
function is fully guarded — a query error, a missing table or a disabled feature
degrades to an **empty list** rather than raising into the caller (the WhatsApp
webhook, the JSON API and the admin dashboard).

Result shape
~~~~~~~~~~~~~
Every recommendation function returns a ``list`` of plain, JSON-serialisable
dicts::

    {
        "product_retailer_id": str,
        "product_name": str,
        "score": float,          # meaning depends on the strategy (see below)
        "price": float,          # optional; most-recent known unit price
    }

Strategies
~~~~~~~~~~~
* :func:`frequently_bought_together` — items that co-occur in the same orders as
  a seed product (score = co-occurrence count).
* :func:`trending` — best sellers by quantity over a recent window (score = qty).
* :func:`personalized` — FBT candidates aggregated across a customer's purchase
  history, excluding what they already own (falls back to :func:`trending`).
* :func:`similar_products` — attribute similarity by product-name token overlap
  (score = number of shared tokens).

The two heaviest queries (:func:`trending` and
:func:`frequently_bought_together`) are memoized through :mod:`utils.cache` with
a short TTL so repeated dashboard / webhook calls stay cheap; the cache layer is
imported lazily and its use is fully guarded.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import config
from utils.logging import logger

__all__ = [
    "frequently_bought_together",
    "trending",
    "personalized",
    "similar_products",
    "recommend_for_whatsapp",
]

# Default memoization TTL (seconds) for the heavier analytics queries.
_CACHE_TTL_SECONDS = 300

# Tokens shorter than this (and pure stop-words) are ignored when comparing
# product names for :func:`similar_products`.
_MIN_TOKEN_LEN = 2
_STOP_TOKENS = frozenset(
    {"the", "and", "for", "with", "set", "pcs", "pc", "size", "pack"}
)


# --------------------------------------------------------------------------
# Feature flag + small helpers
# --------------------------------------------------------------------------

def _enabled() -> bool:
    """Return whether the recommendation surface is switched on."""
    try:
        return bool(config.recommendations_enabled)
    except Exception:  # noqa: BLE001 - a missing flag must never break callers
        return True


def _clamp_limit(limit: int, *, default: int = 5, maximum: int = 50) -> int:
    """Coerce ``limit`` into a sane positive integer."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def _to_float(value: Any) -> Optional[float]:
    """Best-effort float coercion; ``None`` when not numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cutoff(days: int) -> datetime:
    """Return the UTC datetime ``days`` in the past (floored at 1 day)."""
    try:
        window = int(days)
    except (TypeError, ValueError):
        window = 30
    if window <= 0:
        window = 30
    return datetime.now(timezone.utc) - timedelta(days=window)


def _tokens(name: Optional[str]) -> set:
    """Split a product name into a set of comparable lowercase tokens."""
    if not name:
        return set()
    raw = re.split(r"[^0-9a-zA-Z]+", str(name).lower())
    return {
        tok
        for tok in raw
        if len(tok) >= _MIN_TOKEN_LEN and tok not in _STOP_TOKENS
    }


# --------------------------------------------------------------------------
# Lightweight cache memoization (lazy + guarded)
# --------------------------------------------------------------------------

def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    """Return a cached result list for ``key`` (``None`` on miss/any error)."""
    try:
        from utils.cache import cache_get_json

        value = cache_get_json(key)
        if isinstance(value, list):
            return value
    except Exception as exc:  # noqa: BLE001 - cache must never break analytics
        logger.debug("RECO | cache get skipped (%s): %r", key, exc)
    return None


def _cache_set(key: str, value: List[Dict[str, Any]]) -> None:
    """Store ``value`` under ``key`` with the module TTL (best-effort)."""
    try:
        from utils.cache import cache_set_json

        cache_set_json(key, value, _CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("RECO | cache set skipped (%s): %r", key, exc)


# --------------------------------------------------------------------------
# Frequently bought together
# --------------------------------------------------------------------------

def frequently_bought_together(
    retailer_id: str,
    *,
    limit: int = 5,
    tenant_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return products most often bought in the same order as ``retailer_id``.

    The seed item's orders are located, then every *other* line item across
    those orders is counted; the highest co-occurrence counts win. The seed
    item is always excluded from its own recommendations.

    Args:
        retailer_id: The seed product's ``product_retailer_id``.
        limit: Maximum number of recommendations to return.
        tenant_id: When set, only consider orders belonging to this store.

    Returns:
        A ranked list of recommendation dicts (score = co-occurrence count).
        Empty when disabled, on error, or when nothing co-occurs.
    """
    if not _enabled() or not retailer_id:
        return []
    limit = _clamp_limit(limit, default=5)

    cache_key = f"reco:fbt:{tenant_id or 0}:{retailer_id}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        from database.db import session_scope
        from database.models import Order, OrderItem

        with session_scope() as session:
            # Orders (optionally tenant-scoped, never soft-deleted) that
            # contain the seed item.
            order_ids_q = (
                session.query(OrderItem.order_id)
                .join(Order, Order.id == OrderItem.order_id)
                .filter(OrderItem.product_retailer_id == retailer_id)
                .filter(Order.deleted_at.is_(None))
            )
            if tenant_id:
                order_ids_q = order_ids_q.filter(Order.tenant_id == tenant_id)
            order_ids = [row[0] for row in order_ids_q.all()]
            if not order_ids:
                _cache_set(cache_key, [])
                return []

            # Every other line item across those orders.
            rows = (
                session.query(OrderItem)
                .filter(OrderItem.order_id.in_(order_ids))
                .filter(OrderItem.product_retailer_id.isnot(None))
                .filter(OrderItem.product_retailer_id != retailer_id)
                .all()
            )
            results = _rank_cooccurrence(rows)
    except Exception as exc:  # noqa: BLE001 - analytics never raise
        logger.debug("RECO | frequently_bought_together(%s) failed: %r", retailer_id, exc)
        return []

    results = results[:limit]
    _cache_set(cache_key, results)
    return results


def _rank_cooccurrence(rows: List[Any]) -> List[Dict[str, Any]]:
    """Aggregate ORM line items into ranked co-occurrence recommendation dicts."""
    counts: Dict[str, int] = defaultdict(int)
    names: Dict[str, str] = {}
    prices: Dict[str, Optional[float]] = {}
    for item in rows:
        rid = item.product_retailer_id
        if not rid:
            continue
        counts[rid] += 1
        if rid not in names and item.product_name:
            names[rid] = item.product_name
        price = _to_float(getattr(item, "unit_price", None))
        if price is not None:
            prices[rid] = price

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: List[Dict[str, Any]] = []
    for rid, count in ranked:
        entry: Dict[str, Any] = {
            "product_retailer_id": rid,
            "product_name": names.get(rid) or rid,
            "score": float(count),
        }
        if prices.get(rid) is not None:
            entry["price"] = prices[rid]
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# Trending
# --------------------------------------------------------------------------

def trending(
    *,
    limit: int = 10,
    days: int = 30,
    tenant_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return the best-selling products over the last ``days`` days.

    Ranking is by total quantity sold; the score equals that summed quantity.
    Only non-soft-deleted orders (optionally tenant-scoped) are considered.

    Args:
        limit: Maximum number of products to return.
        days: Size of the look-back window, in days.
        tenant_id: When set, only consider orders belonging to this store.

    Returns:
        A ranked list of recommendation dicts (score = quantity). Empty when
        disabled, on error, or when there is no recent sales activity.
    """
    if not _enabled():
        return []
    limit = _clamp_limit(limit, default=10)

    cache_key = f"reco:trending:{tenant_id or 0}:{days}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        from sqlalchemy import func

        from database.db import session_scope
        from database.models import Order, OrderItem

        cutoff = _cutoff(days)
        with session_scope() as session:
            qty = func.sum(OrderItem.quantity)
            q = (
                session.query(
                    OrderItem.product_retailer_id,
                    func.max(OrderItem.product_name),
                    func.max(OrderItem.unit_price),
                    qty,
                )
                .join(Order, Order.id == OrderItem.order_id)
                .filter(Order.deleted_at.is_(None))
                .filter(Order.created_at >= cutoff)
                .filter(OrderItem.product_retailer_id.isnot(None))
            )
            if tenant_id:
                q = q.filter(Order.tenant_id == tenant_id)
            q = (
                q.group_by(OrderItem.product_retailer_id)
                .order_by(qty.desc())
                .limit(limit)
            )
            results = _rows_to_results(q.all())
    except Exception as exc:  # noqa: BLE001 - analytics never raise
        logger.debug("RECO | trending() failed: %r", exc)
        return []

    _cache_set(cache_key, results)
    return results


def _rows_to_results(rows: List[Any]) -> List[Dict[str, Any]]:
    """Turn ``(retailer_id, name, price, score)`` tuples into result dicts."""
    out: List[Dict[str, Any]] = []
    for rid, name, price, score in rows:
        if not rid:
            continue
        entry: Dict[str, Any] = {
            "product_retailer_id": rid,
            "product_name": name or rid,
            "score": float(_to_float(score) or 0.0),
        }
        price_f = _to_float(price)
        if price_f is not None:
            entry["price"] = price_f
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# Personalized
# --------------------------------------------------------------------------

def personalized(
    wa_number: str,
    *,
    limit: int = 10,
    tenant_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return personalized recommendations for a customer.

    The customer's previously purchased products are gathered, then
    frequently-bought-together candidates are aggregated across all of them
    (co-occurrence over *every* customer's orders). Items the customer already
    owns are excluded. When the customer has no purchase history, this falls
    back to :func:`trending`.

    Args:
        wa_number: The customer's WhatsApp number.
        limit: Maximum number of recommendations to return.
        tenant_id: When set, only consider orders belonging to this store.

    Returns:
        A ranked list of recommendation dicts (score = summed co-occurrence).
        Empty only when disabled or on error.
    """
    if not _enabled() or not wa_number:
        return []
    limit = _clamp_limit(limit, default=10)

    try:
        owned = _customer_products(wa_number, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("RECO | personalized(%s) history load failed: %r", wa_number, exc)
        owned = set()

    if not owned:
        # Cold-start: no history -> surface what's hot right now.
        return trending(limit=limit, tenant_id=tenant_id)

    # Aggregate FBT scores across each owned item, then drop owned items.
    scores: Dict[str, float] = defaultdict(float)
    meta: Dict[str, Dict[str, Any]] = {}
    for seed in owned:
        for rec in frequently_bought_together(seed, limit=limit, tenant_id=tenant_id):
            rid = rec.get("product_retailer_id")
            if not rid or rid in owned:
                continue
            scores[rid] += float(rec.get("score") or 0.0)
            if rid not in meta:
                meta[rid] = rec

    if not scores:
        # History exists but nothing co-occurs yet -> fall back to trending.
        trend = [r for r in trending(limit=limit + len(owned), tenant_id=tenant_id)
                 if r.get("product_retailer_id") not in owned]
        return trend[:limit]

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    out: List[Dict[str, Any]] = []
    for rid, score in ranked[:limit]:
        base = meta.get(rid, {})
        entry: Dict[str, Any] = {
            "product_retailer_id": rid,
            "product_name": base.get("product_name") or rid,
            "score": float(score),
        }
        if base.get("price") is not None:
            entry["price"] = base["price"]
        out.append(entry)
    return out


def _customer_products(
    wa_number: str, *, tenant_id: Optional[int] = None
) -> set:
    """Return the set of ``product_retailer_id`` the customer has purchased."""
    from database.db import session_scope
    from database.models import Order, OrderItem

    with session_scope() as session:
        q = (
            session.query(OrderItem.product_retailer_id)
            .join(Order, Order.id == OrderItem.order_id)
            .filter(Order.wa_number == wa_number)
            .filter(Order.deleted_at.is_(None))
            .filter(OrderItem.product_retailer_id.isnot(None))
        )
        if tenant_id:
            q = q.filter(Order.tenant_id == tenant_id)
        return {row[0] for row in q.all() if row[0]}


# --------------------------------------------------------------------------
# Similar products (attribute similarity)
# --------------------------------------------------------------------------

def similar_products(
    retailer_id: str,
    *,
    limit: int = 5,
    tenant_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return products similar to ``retailer_id`` by attribute overlap.

    Similarity is computed cheaply in Python from the seed item's
    ``product_name`` tokens (and ``product_type`` when the column exists),
    scoring every other distinct product by the number of shared tokens. This
    keeps the query trivial and portable across databases.

    Args:
        retailer_id: The seed product's ``product_retailer_id``.
        limit: Maximum number of similar products to return.
        tenant_id: When set, only consider orders belonging to this store.

    Returns:
        A ranked list of recommendation dicts (score = shared-token count).
        Empty when disabled, on error, or when nothing overlaps.
    """
    if not _enabled() or not retailer_id:
        return []
    limit = _clamp_limit(limit, default=5)

    try:
        from database.db import session_scope
        from database.models import Order, OrderItem

        with session_scope() as session:
            base_q = (
                session.query(OrderItem)
                .join(Order, Order.id == OrderItem.order_id)
                .filter(Order.deleted_at.is_(None))
                .filter(OrderItem.product_retailer_id.isnot(None))
            )
            if tenant_id:
                base_q = base_q.filter(Order.tenant_id == tenant_id)

            seed = (
                base_q.filter(OrderItem.product_retailer_id == retailer_id)
                .order_by(OrderItem.id.desc())
                .first()
            )
            if seed is None:
                return []
            seed_tokens = _tokens(seed.product_name) | _tokens(
                getattr(seed, "product_type", None)
            )
            if not seed_tokens:
                return []

            # Score the most-recent representative row per other product.
            best: Dict[str, Dict[str, Any]] = {}
            for item in base_q.filter(
                OrderItem.product_retailer_id != retailer_id
            ).order_by(OrderItem.id.desc()).all():
                rid = item.product_retailer_id
                if not rid or rid in best:
                    continue
                tokens = _tokens(item.product_name) | _tokens(
                    getattr(item, "product_type", None)
                )
                overlap = len(seed_tokens & tokens)
                if overlap <= 0:
                    continue
                entry: Dict[str, Any] = {
                    "product_retailer_id": rid,
                    "product_name": item.product_name or rid,
                    "score": float(overlap),
                }
                price = _to_float(getattr(item, "unit_price", None))
                if price is not None:
                    entry["price"] = price
                best[rid] = entry
    except Exception as exc:  # noqa: BLE001 - analytics never raise
        logger.debug("RECO | similar_products(%s) failed: %r", retailer_id, exc)
        return []

    ranked = sorted(
        best.values(),
        key=lambda e: (-e["score"], e["product_retailer_id"]),
    )
    return ranked[:limit]


# --------------------------------------------------------------------------
# WhatsApp cross-sell hook
# --------------------------------------------------------------------------

def recommend_for_whatsapp(
    wa_number: str, *, limit: int = 5
) -> List[Dict[str, Any]]:
    """Return a compact recommendation list for a WhatsApp cross-sell message.

    Thin wrapper over :func:`personalized` intended to be called by the
    WhatsApp webhook after an order is placed, or on a "recommend" intent. The
    output is trimmed to the fields a catalog/product message needs.

    Args:
        wa_number: The customer's WhatsApp number.
        limit: Maximum number of products to suggest.

    Returns:
        A list of ``{"product_retailer_id", "product_name", "score", "price"?}``
        dicts (never raises; empty list when nothing to suggest).
    """
    limit = _clamp_limit(limit, default=5)
    try:
        recs = personalized(wa_number, limit=limit)
    except Exception as exc:  # noqa: BLE001 - the hook must never break the webhook
        logger.debug("RECO | recommend_for_whatsapp(%s) failed: %r", wa_number, exc)
        return []

    compact: List[Dict[str, Any]] = []
    for rec in recs[:limit]:
        entry: Dict[str, Any] = {
            "product_retailer_id": rec.get("product_retailer_id"),
            "product_name": rec.get("product_name"),
            "score": float(rec.get("score") or 0.0),
        }
        if rec.get("price") is not None:
            entry["price"] = rec["price"]
        compact.append(entry)
    return compact
