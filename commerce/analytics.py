"""
commerce/analytics.py
----------------------
Read-side analytics for the v6.0 Enterprise Commerce order platform.

Every function is a *pure read*: it opens its own :func:`session_scope`, runs a
single SQLAlchemy 2.0 aggregation (or fetches a compact row set and rolls it up
in Python for cross-database date bucketing), and returns plain, JSON-friendly
dicts/lists. None of these functions ever raise — on any error they log and
return a safe, well-formed default so the admin dashboard and its JSON API can
always render.

Revenue is defined uniformly across this module as the sum of
``Order.total_amount`` for orders that are either paid (``payment_status ==
'paid'``) or delivered (``status == 'delivered'``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import func, or_, select

from database.db import session_scope
from database.models import Order, OrderItem
from utils.logging import logger

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Statuses considered "in progress" (neither closed nor cancelled/refunded).
_OPEN_STATUSES = ("received", "confirmed", "packed", "shipped", "out_for_delivery")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _revenue_predicate():
    """SQLAlchemy predicate for orders that count toward revenue."""
    return or_(Order.payment_status == "paid", Order.status == "delivered")


def _num(value: Any) -> float:
    """Coerce a possibly-Decimal/None aggregate to a rounded float."""
    if value is None:
        return 0.0
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _as_dt(value: Any) -> datetime:
    """Best-effort coercion of a stored ``created_at`` to an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return _now()


# ---------------------------------------------------------------------------
# Headline summary
# ---------------------------------------------------------------------------

def order_summary() -> Dict[str, Any]:
    """Return the headline KPI card metrics for the orders dashboard.

    Keys: ``today_orders``, ``month_orders``, ``pending_orders``,
    ``delivered_orders``, ``cancelled_orders``, ``revenue``,
    ``avg_order_value``, ``total_orders``.
    """
    default: Dict[str, Any] = {
        "today_orders": 0,
        "month_orders": 0,
        "pending_orders": 0,
        "delivered_orders": 0,
        "cancelled_orders": 0,
        "revenue": 0.0,
        "avg_order_value": 0.0,
        "total_orders": 0,
    }
    try:
        now = _now()
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_month = start_of_today.replace(day=1)

        with session_scope() as session:
            total_orders = session.scalar(select(func.count(Order.id))) or 0
            today_orders = session.scalar(
                select(func.count(Order.id)).where(Order.created_at >= start_of_today)
            ) or 0
            month_orders = session.scalar(
                select(func.count(Order.id)).where(Order.created_at >= start_of_month)
            ) or 0
            pending_orders = session.scalar(
                select(func.count(Order.id)).where(Order.status.in_(_OPEN_STATUSES))
            ) or 0
            delivered_orders = session.scalar(
                select(func.count(Order.id)).where(Order.status == "delivered")
            ) or 0
            cancelled_orders = session.scalar(
                select(func.count(Order.id)).where(Order.status == "cancelled")
            ) or 0

            revenue = session.scalar(
                select(func.coalesce(func.sum(Order.total_amount), 0)).where(
                    _revenue_predicate()
                )
            )
            revenue_orders = session.scalar(
                select(func.count(Order.id)).where(_revenue_predicate())
            ) or 0

        revenue = _num(revenue)
        avg_order_value = round(revenue / revenue_orders, 2) if revenue_orders else 0.0
        return {
            "today_orders": int(today_orders),
            "month_orders": int(month_orders),
            "pending_orders": int(pending_orders),
            "delivered_orders": int(delivered_orders),
            "cancelled_orders": int(cancelled_orders),
            "revenue": revenue,
            "avg_order_value": avg_order_value,
            "total_orders": int(total_orders),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | order_summary failed: %s", exc)
        return default


# ---------------------------------------------------------------------------
# Top products / customers
# ---------------------------------------------------------------------------

def top_products(limit: int = 10) -> List[Dict[str, Any]]:
    """Return the best-selling products as ``[{name, qty, revenue}]``.

    Grouped by :attr:`OrderItem.product_name`, ordered by total quantity sold.
    """
    try:
        with session_scope() as session:
            rows = session.execute(
                select(
                    OrderItem.product_name,
                    func.coalesce(func.sum(OrderItem.quantity), 0),
                    func.coalesce(func.sum(OrderItem.line_total), 0),
                )
                .group_by(OrderItem.product_name)
                .order_by(func.sum(OrderItem.quantity).desc())
                .limit(limit)
            ).all()
        return [
            {
                "name": name or "Unknown",
                "qty": int(qty or 0),
                "revenue": _num(revenue),
            }
            for name, qty, revenue in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | top_products failed: %s", exc)
        return []


def top_customers(limit: int = 10) -> List[Dict[str, Any]]:
    """Return the highest-spending customers as ``[{wa_number, customer_name,
    orders, spend}]``, grouped by WhatsApp number and ordered by spend."""
    try:
        with session_scope() as session:
            rows = session.execute(
                select(
                    Order.wa_number,
                    func.max(Order.customer_name),
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.total_amount), 0),
                )
                .group_by(Order.wa_number)
                .order_by(func.sum(Order.total_amount).desc())
                .limit(limit)
            ).all()
        return [
            {
                "wa_number": wa_number or "",
                "customer_name": name or "",
                "orders": int(orders or 0),
                "spend": _num(spend),
            }
            for wa_number, name, orders, spend in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | top_customers failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

def sales_by_region() -> Dict[str, List[Dict[str, Any]]]:
    """Return orders/revenue grouped by state and by city.

    Shape: ``{"by_state": [{state, orders, revenue}], "by_city": [...]}``.
    """
    default = {"by_state": [], "by_city": []}
    try:
        with session_scope() as session:
            state_rows = session.execute(
                select(
                    Order.state,
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.total_amount), 0),
                )
                .group_by(Order.state)
                .order_by(func.sum(Order.total_amount).desc())
            ).all()
            city_rows = session.execute(
                select(
                    Order.city,
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.total_amount), 0),
                )
                .group_by(Order.city)
                .order_by(func.sum(Order.total_amount).desc())
            ).all()

        by_state = [
            {"state": state or "Unknown", "orders": int(o or 0), "revenue": _num(r)}
            for state, o, r in state_rows
        ]
        by_city = [
            {"city": city or "Unknown", "orders": int(o or 0), "revenue": _num(r)}
            for city, o, r in city_rows
        ]
        return {"by_state": by_state, "by_city": by_city}
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | sales_by_region failed: %s", exc)
        return default


# ---------------------------------------------------------------------------
# Time series (Python-side bucketing for cross-DB portability)
# ---------------------------------------------------------------------------

def _series(rows: List[Any], keyfn, order_keys: List[str]) -> List[Dict[str, Any]]:
    """Bucket ``(created_at, total_amount)`` rows into period totals.

    ``keyfn`` maps a datetime to a period label; ``order_keys`` is the ordered
    list of labels to emit (so empty periods appear as zeros).
    """
    buckets: Dict[str, Dict[str, float]] = {
        key: {"orders": 0, "revenue": 0.0} for key in order_keys
    }
    for created_at, total in rows:
        key = keyfn(_as_dt(created_at))
        bucket = buckets.setdefault(key, {"orders": 0, "revenue": 0.0})
        bucket["orders"] += 1
        bucket["revenue"] += _num(total)
    return [
        {
            "period": key,
            "orders": int(buckets[key]["orders"]),
            "revenue": round(buckets[key]["revenue"], 2),
        }
        for key in order_keys
    ]


def daily_series(days: int = 30) -> List[Dict[str, Any]]:
    """Return ``[{period, orders, revenue}]`` for each of the last ``days`` days."""
    try:
        now = _now()
        start = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(days=days - 1))
        order_keys = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        with session_scope() as session:
            rows = session.execute(
                select(Order.created_at, Order.total_amount).where(Order.created_at >= start)
            ).all()
        return _series(rows, lambda dt: dt.strftime("%Y-%m-%d"), order_keys)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | daily_series failed: %s", exc)
        return []


def monthly_series(months: int = 12) -> List[Dict[str, Any]]:
    """Return ``[{period, orders, revenue}]`` for each of the last ``months``."""
    try:
        now = _now()
        # Build the ordered list of YYYY-MM labels ending with the current month.
        order_keys: List[str] = []
        year, month = now.year, now.month
        for _ in range(months):
            order_keys.append(f"{year:04d}-{month:02d}")
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        order_keys.reverse()
        start_label = order_keys[0]
        start_year, start_month = int(start_label[:4]), int(start_label[5:7])
        start = datetime(start_year, start_month, 1, tzinfo=timezone.utc)
        with session_scope() as session:
            rows = session.execute(
                select(Order.created_at, Order.total_amount).where(Order.created_at >= start)
            ).all()
        return _series(rows, lambda dt: dt.strftime("%Y-%m"), order_keys)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | monthly_series failed: %s", exc)
        return []


def yearly_series() -> List[Dict[str, Any]]:
    """Return ``[{period, orders, revenue}]`` for every year that has orders."""
    try:
        with session_scope() as session:
            rows = session.execute(
                select(Order.created_at, Order.total_amount)
            ).all()
        years = sorted({_as_dt(created_at).strftime("%Y") for created_at, _ in rows})
        if not years:
            return []
        return _series(rows, lambda dt: dt.strftime("%Y"), years)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | yearly_series failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def conversion_rate() -> float:
    """Return paid orders as a percentage of all orders (0 when there are none)."""
    try:
        with session_scope() as session:
            total = session.scalar(select(func.count(Order.id))) or 0
            paid = session.scalar(
                select(func.count(Order.id)).where(Order.payment_status == "paid")
            ) or 0
        if not total:
            return 0.0
        return round(paid / total * 100.0, 2)
    except Exception as exc:  # noqa: BLE001
        logger.error("COMMERCE-ANALYTICS | conversion_rate failed: %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# Aggregate bundle (for the analytics page + JSON API)
# ---------------------------------------------------------------------------

def analytics_bundle() -> Dict[str, Any]:
    """Assemble every analytics widget into one dict for templates / the API."""
    return {
        "summary": order_summary(),
        "top_products": top_products(),
        "top_customers": top_customers(),
        "regions": sales_by_region(),
        "daily": daily_series(30),
        "monthly": monthly_series(12),
        "yearly": yearly_series(),
        "conversion_rate": conversion_rate(),
    }
