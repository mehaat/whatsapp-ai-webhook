"""
commerce/crm.py
----------------
The v6.1 Customer CRM read/write service.

This layer turns the raw commerce order history plus the CRM enrichment tables
(:class:`CrmProfile`, :class:`CustomerNote`) into simple, serializable customer
records for the admin dashboard: a per-customer aggregate list, a full profile
(lifetime value, order history, notes, tags, segment) and small write helpers
for notes, tags and segment.

Design contract, mirroring :mod:`commerce.service`:

    * Pure SQLAlchemy over the shared engine (SQLite by default, PostgreSQL via
      ``DATABASE_URL``). Reads are grouped/aggregated in the database.
    * Every function is defensive — it **never raises**. On any error it logs
      and returns a safe empty default (``[]``, ``0``, ``None`` or a minimal
      dict), so the admin UI degrades gracefully instead of 500-ing.
    * Results are plain ``dict``/``list`` structures (Decimals coerced to
      ``float``, datetimes to ISO-8601 strings); callers never hold a live ORM
      session.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from utils.logging import logger

# Segment thresholds (shared by the suggestion + recompute logic).
_VIP_LTV = 25000.0
_VIP_ORDERS = 5
_REPEAT_ORDERS = 2


# --------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------

def _f(value: Any) -> float:
    """Coerce a Decimal/None/str to ``float`` for JSON-safe output."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso(value: Any) -> Optional[str]:
    """Coerce a datetime to ISO-8601, passing through strings and ``None``."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value or None
    return None


def _split_tags(raw: Optional[str]) -> List[str]:
    """Split a comma-separated tag string into a clean list (order-preserving)."""
    if not raw:
        return []
    seen: List[str] = []
    for part in str(raw).split(","):
        tag = part.strip()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _join_tags(tags: Optional[List[str]]) -> str:
    """Normalise a list of tags to a de-duplicated comma-separated string."""
    if not tags:
        return ""
    cleaned: List[str] = []
    for tag in tags:
        value = str(tag).strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return ",".join(cleaned)


def suggest_segment(lifetime_value: float, orders_count: int) -> str:
    """Return a heuristic segment for a customer.

    * ``"vip"`` when lifetime value ≥ 25,000 **or** there are ≥ 5 orders.
    * ``"repeat"`` when there are ≥ 2 orders.
    * ``"new"`` otherwise.
    """
    if _f(lifetime_value) >= _VIP_LTV or int(orders_count or 0) >= _VIP_ORDERS:
        return "vip"
    if int(orders_count or 0) >= _REPEAT_ORDERS:
        return "repeat"
    return "new"


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def list_customers(
    *,
    query: Optional[str] = None,
    segment: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return per-customer aggregate rows, richest (by lifetime value) first.

    Each row aggregates the customer's orders (count, summed lifetime value,
    most-recent order timestamp) and is enriched with the CRM profile (display
    name, tags, segment) and the WhatsApp customer's profile name.

    Args:
        query: Free-text filter matched against the WhatsApp number, the order
            customer name, the CRM display name and the WhatsApp profile name.
        segment: Restrict to customers whose stored CRM segment equals this.
        tag: Restrict to customers whose CRM tags contain this substring.
        limit: Maximum number of rows to return.
        offset: Number of rows to skip (for pagination).

    Returns:
        A list of dicts with keys ``wa_number``, ``name``, ``orders_count``,
        ``lifetime_value``, ``last_order_at``, ``segment`` and ``tags`` (list).
        Empty on any error.
    """
    try:
        from database.db import session_scope
        from database.models import CrmProfile, Customer, Order

        with session_scope() as session:
            q = (
                session.query(
                    Order.wa_number.label("wa_number"),
                    func.count(Order.id).label("orders_count"),
                    func.coalesce(func.sum(Order.total_amount), 0).label("lifetime_value"),
                    func.max(Order.created_at).label("last_order_at"),
                    func.max(Order.customer_name).label("order_name"),
                    CrmProfile.display_name.label("display_name"),
                    CrmProfile.tags.label("tags"),
                    CrmProfile.segment.label("segment"),
                    Customer.profile_name.label("profile_name"),
                )
                .outerjoin(CrmProfile, CrmProfile.wa_number == Order.wa_number)
                .outerjoin(Customer, Customer.wa_number == Order.wa_number)
            )
            q = _apply_filters(q, Order, CrmProfile, query, segment, tag)
            q = q.group_by(
                Order.wa_number,
                CrmProfile.display_name,
                CrmProfile.tags,
                CrmProfile.segment,
                Customer.profile_name,
            )
            q = q.order_by(func.coalesce(func.sum(Order.total_amount), 0).desc())
            q = q.limit(max(1, int(limit))).offset(max(0, int(offset)))

            rows: List[Dict[str, Any]] = []
            for r in q.all():
                orders_count = int(r.orders_count or 0)
                ltv = _f(r.lifetime_value)
                rows.append(
                    {
                        "wa_number": r.wa_number,
                        "name": r.display_name or r.profile_name or r.order_name or "",
                        "orders_count": orders_count,
                        "lifetime_value": ltv,
                        "last_order_at": _iso(r.last_order_at),
                        "segment": r.segment or suggest_segment(ltv, orders_count),
                        "tags": _split_tags(r.tags),
                    }
                )
            return rows
    except Exception as exc:  # noqa: BLE001 - CRM reads must never break the UI
        logger.error("CRM | list_customers failed: %s", exc)
        return []


def count_customers(
    *,
    query: Optional[str] = None,
    segment: Optional[str] = None,
    tag: Optional[str] = None,
) -> int:
    """Return the number of distinct customers matching the given filters."""
    try:
        from database.db import session_scope
        from database.models import CrmProfile, Customer, Order

        with session_scope() as session:
            q = (
                session.query(func.count(func.distinct(Order.wa_number)))
                .outerjoin(CrmProfile, CrmProfile.wa_number == Order.wa_number)
                .outerjoin(Customer, Customer.wa_number == Order.wa_number)
            )
            q = _apply_filters(q, Order, CrmProfile, query, segment, tag)
            return int(q.scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.error("CRM | count_customers failed: %s", exc)
        return 0


def get_customer(wa_number: str) -> Optional[Dict[str, Any]]:
    """Return the full CRM profile for one customer, or ``None`` if unknown.

    The returned dict contains the aggregated order metrics (orders count,
    lifetime value, first/last order timestamps, average order value), the CRM
    tags (list) and segment (falling back to a suggested segment when none is
    stored), the customer's notes and their recent order history.

    Args:
        wa_number: The customer's WhatsApp number (exact match).

    Returns:
        A profile dict, or ``None`` when the customer has no orders/profile or
        on error.
    """
    wa_number = (wa_number or "").strip()
    if not wa_number:
        return None
    try:
        from database.db import session_scope
        from database.models import CrmProfile, Customer, CustomerNote, Order
        from commerce.service import order_service

        with session_scope() as session:
            agg = (
                session.query(
                    func.count(Order.id).label("orders_count"),
                    func.coalesce(func.sum(Order.total_amount), 0).label("lifetime_value"),
                    func.min(Order.created_at).label("first_order_at"),
                    func.max(Order.created_at).label("last_order_at"),
                    func.max(Order.customer_name).label("order_name"),
                )
                .filter(Order.wa_number == wa_number)
                .one()
            )
            orders_count = int(agg.orders_count or 0)
            lifetime_value = _f(agg.lifetime_value)

            profile = session.get(CrmProfile, wa_number)
            customer = (
                session.query(Customer).filter(Customer.wa_number == wa_number).first()
            )

            # A customer is "known" if they have orders, a CRM profile or notes.
            note_rows = (
                session.query(CustomerNote)
                .filter(CustomerNote.wa_number == wa_number)
                .order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc())
                .all()
            )
            if orders_count == 0 and profile is None and customer is None and not note_rows:
                return None

            name = (
                (profile.display_name if profile else None)
                or (customer.profile_name if customer else None)
                or agg.order_name
                or ""
            )
            segment = (profile.segment if profile else None) or suggest_segment(
                lifetime_value, orders_count
            )
            tags = _split_tags(profile.tags if profile else None)
            avg_order_value = (lifetime_value / orders_count) if orders_count else 0.0

            notes = [
                {
                    "author": n.author,
                    "note": n.note,
                    "created_at": _iso(n.created_at),
                }
                for n in note_rows
            ]

        # Recent order history — fetched outside the session via the service.
        # ``list_orders`` matches ``query`` as a substring, so filter to the
        # exact WhatsApp number to avoid accidental cross-customer matches.
        orders = [
            o
            for o in order_service.list_orders(query=wa_number, limit=50)
            if o.get("wa_number") == wa_number
        ]

        return {
            "wa_number": wa_number,
            "name": name,
            "orders_count": orders_count,
            "lifetime_value": lifetime_value,
            "first_order_at": _iso(agg.first_order_at),
            "last_order_at": _iso(agg.last_order_at),
            "avg_order_value": avg_order_value,
            "tags": tags,
            "segment": segment,
            "notes": notes,
            "orders": orders,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("CRM | get_customer(%s) failed: %s", wa_number, exc)
        return None


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def add_note(wa_number: str, note: str, author: str = "admin") -> Dict[str, Any]:
    """Attach a free-text note to a customer and return the stored note.

    Args:
        wa_number: The customer's WhatsApp number.
        note: The note body.
        author: Who wrote the note (defaults to ``"admin"``).

    Returns:
        The stored note as ``{"author", "note", "created_at"}``, or an empty
        dict on error / when ``note`` is blank.
    """
    wa_number = (wa_number or "").strip()
    note = (note or "").strip()
    if not wa_number or not note:
        return {}
    try:
        from database.db import session_scope
        from database.models import CustomerNote

        with session_scope() as session:
            row = CustomerNote(wa_number=wa_number, author=(author or "admin"), note=note)
            session.add(row)
            session.flush()
            result = {
                "id": row.id,
                "author": row.author,
                "note": row.note,
                "created_at": _iso(row.created_at),
            }
        logger.info("CRM | Note added for %s by %s", wa_number, author)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("CRM | add_note(%s) failed: %s", wa_number, exc)
        return {}


def set_tags(wa_number: str, tags: List[str]) -> Dict[str, Any]:
    """Replace a customer's CRM tags (upserting the profile) and return it.

    Args:
        wa_number: The customer's WhatsApp number.
        tags: The new tag list; stored comma-separated and de-duplicated.

    Returns:
        The updated profile dict (``wa_number``, ``tags`` list, ``segment``),
        or an empty dict on error.
    """
    wa_number = (wa_number or "").strip()
    if not wa_number:
        return {}
    try:
        joined = _join_tags(tags)
        profile = _upsert_profile(wa_number, tags=joined)
        logger.info("CRM | Tags for %s -> %s", wa_number, joined or "(none)")
        return profile
    except Exception as exc:  # noqa: BLE001
        logger.error("CRM | set_tags(%s) failed: %s", wa_number, exc)
        return {}


def set_segment(wa_number: str, segment: str) -> Dict[str, Any]:
    """Set a customer's CRM segment (upserting the profile) and return it."""
    wa_number = (wa_number or "").strip()
    if not wa_number:
        return {}
    try:
        segment_value = (segment or "").strip().lower() or None
        profile = _upsert_profile(wa_number, segment=segment_value)
        logger.info("CRM | Segment for %s -> %s", wa_number, segment_value or "(auto)")
        return profile
    except Exception as exc:  # noqa: BLE001
        logger.error("CRM | set_segment(%s) failed: %s", wa_number, exc)
        return {}


def recompute_profile(wa_number: str) -> Dict[str, Any]:
    """Recompute and cache a customer's CRM profile from their orders.

    Refreshes the cached ``lifetime_value``, ``orders_count`` and
    ``display_name`` (from the latest order) on the :class:`CrmProfile`,
    creating the row if needed.

    Returns:
        The updated profile dict, or an empty dict on error.
    """
    wa_number = (wa_number or "").strip()
    if not wa_number:
        return {}
    try:
        from database.db import session_scope
        from database.models import Order

        with session_scope() as session:
            agg = (
                session.query(
                    func.count(Order.id).label("orders_count"),
                    func.coalesce(func.sum(Order.total_amount), 0).label("lifetime_value"),
                )
                .filter(Order.wa_number == wa_number)
                .one()
            )
            latest = (
                session.query(Order)
                .filter(Order.wa_number == wa_number)
                .order_by(Order.created_at.desc(), Order.id.desc())
                .first()
            )
            display_name = latest.customer_name if latest else None

        profile = _upsert_profile(
            wa_number,
            lifetime_value=_f(agg.lifetime_value),
            orders_count=int(agg.orders_count or 0),
            display_name=display_name,
        )
        logger.info(
            "CRM | Recomputed profile for %s (orders=%s ltv=%.2f)",
            wa_number,
            profile.get("orders_count"),
            _f(profile.get("lifetime_value")),
        )
        return profile
    except Exception as exc:  # noqa: BLE001
        logger.error("CRM | recompute_profile(%s) failed: %s", wa_number, exc)
        return {}


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

def _apply_filters(q, Order, CrmProfile, query, segment, tag):
    """Apply the shared list/count WHERE clauses to a query."""
    if query:
        from database.models import Customer  # local: keep import cost off hot path

        like = f"%{query}%"
        q = q.filter(
            (Order.wa_number.ilike(like))
            | (Order.customer_name.ilike(like))
            | (CrmProfile.display_name.ilike(like))
            | (Customer.profile_name.ilike(like))
        )
    if segment:
        q = q.filter(CrmProfile.segment == segment)
    if tag:
        q = q.filter(CrmProfile.tags.ilike(f"%{tag}%"))
    return q


def _upsert_profile(wa_number: str, **fields: Any) -> Dict[str, Any]:
    """Create or update a :class:`CrmProfile` row and return it serialized.

    Only keys explicitly present in ``fields`` are written, so partial updates
    (e.g. tags only) never clobber unrelated columns.
    """
    from database.db import session_scope
    from database.models import CrmProfile

    with session_scope() as session:
        profile = session.get(CrmProfile, wa_number)
        if profile is None:
            profile = CrmProfile(wa_number=wa_number)
            session.add(profile)
        for key, value in fields.items():
            setattr(profile, key, value)
        session.flush()
        return _profile_to_dict(profile)


def _profile_to_dict(profile) -> Dict[str, Any]:
    """Serialize a :class:`CrmProfile` ORM row to a plain dict."""
    return {
        "wa_number": profile.wa_number,
        "display_name": profile.display_name,
        "tags": _split_tags(profile.tags),
        "segment": profile.segment,
        "lifetime_value": _f(profile.lifetime_value),
        "orders_count": int(profile.orders_count or 0),
        "created_at": _iso(profile.created_at),
        "updated_at": _iso(profile.updated_at),
    }
