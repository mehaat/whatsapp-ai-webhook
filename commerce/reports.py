"""
commerce/reports.py
--------------------
v7.0 business reports for the Admin Dashboard. Each report reads the durable
commerce tables (orders, order items, payments, inventory reservations) and
returns a uniform, export-friendly shape::

    {"columns": [str, ...], "rows": [[...], ...], "summary": {...}}

Design notes:
    * Every function is defensive and **never raises** — on any error it logs and
      returns an empty report (``columns``/``rows`` present, empty ``summary``),
      so an admin page or export can render gracefully.
    * Aggregation is done in Python after a bounded query, keeping the reports
      identical across SQLite and PostgreSQL (no DB-specific date functions).
    * Monetary values are coerced to ``float`` (ORM ``Numeric`` yields
      ``Decimal``) so the rows are JSON/CSV/XLSX friendly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from database.db import session_scope
from database.models import (
    InventoryReservation,
    Order,
    OrderItem,
    Payment,
)
from utils.logging import logger


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _f(value: Any) -> float:
    """Coerce a Decimal/None/str to a plain float (never raises)."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _empty(columns: List[str]) -> Dict[str, Any]:
    """A well-formed empty report with the given columns."""
    return {"columns": list(columns), "rows": [], "summary": {}}


def _parse_date(value: Optional[str], *, end: bool = False) -> Optional[datetime]:
    """Parse a ``YYYY-MM-DD`` (or ISO) string to an aware UTC datetime.

    Returns ``None`` when ``value`` is falsy or unparseable. When ``end`` is
    True a bare date is pushed to the end of that day so the bound is inclusive.
    """
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d" and end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning("REPORTS | Unparseable date %r; ignoring", value)
    return None


def _orders_in_range(
    session, date_from: Optional[str], date_to: Optional[str]
) -> List[Order]:
    """Return orders whose ``created_at`` falls in the (optional) range."""
    q = session.query(Order)
    lo = _parse_date(date_from)
    hi = _parse_date(date_to, end=True)
    if lo is not None:
        q = q.filter(Order.created_at >= lo)
    if hi is not None:
        q = q.filter(Order.created_at <= hi)
    return q.order_by(Order.created_at.asc(), Order.id.asc()).all()


def _is_revenue(order: Order) -> bool:
    """A revenue-recognised order: paid, or delivered."""
    return order.payment_status == "paid" or order.status == "delivered"


# --------------------------------------------------------------------------
# GST report
# --------------------------------------------------------------------------

def gst_report(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    """Per-order GST / tax breakdown over an optional date range.

    Columns: order_number, date, customer, taxable_value, tax, total. The
    taxable value is ``subtotal - discount``; ``tax`` and ``total`` come from the
    order. The summary carries the range totals plus the order count.
    """
    columns = ["order_number", "date", "customer", "taxable_value", "tax", "total"]
    try:
        rows: List[List[Any]] = []
        t_taxable = t_tax = t_total = 0.0
        with session_scope() as session:
            for o in _orders_in_range(session, date_from, date_to):
                taxable = _f(o.subtotal) - _f(o.discount)
                tax = _f(o.tax)
                total = _f(o.total_amount)
                t_taxable += taxable
                t_tax += tax
                t_total += total
                rows.append([
                    o.order_number,
                    o.created_at.date().isoformat() if o.created_at else "",
                    o.customer_name or o.wa_number or "",
                    round(taxable, 2),
                    round(tax, 2),
                    round(total, 2),
                ])
        return {
            "columns": columns,
            "rows": rows,
            "summary": {
                "orders": len(rows),
                "taxable_value": round(t_taxable, 2),
                "tax": round(t_tax, 2),
                "total": round(t_total, 2),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("REPORTS | gst_report failed: %s", exc)
        return _empty(columns)


# --------------------------------------------------------------------------
# Sales report
# --------------------------------------------------------------------------

def sales_report(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    group: str = "day",
) -> Dict[str, Any]:
    """Sales grouped by ``day`` or ``month``.

    Columns: period, orders, revenue, avg_order_value. ``revenue`` sums the
    totals of revenue-recognised orders (paid or delivered); ``orders`` counts
    every order in the period; ``avg_order_value`` = revenue / revenue-orders.
    """
    columns = ["period", "orders", "revenue", "avg_order_value"]
    group = (group or "day").strip().lower()
    if group not in {"day", "month"}:
        group = "day"
    try:
        buckets: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"orders": 0, "revenue": 0.0, "rev_orders": 0}
        )
        with session_scope() as session:
            for o in _orders_in_range(session, date_from, date_to):
                if o.created_at is None:
                    continue
                key = (
                    o.created_at.strftime("%Y-%m")
                    if group == "month"
                    else o.created_at.date().isoformat()
                )
                b = buckets[key]
                b["orders"] += 1
                if _is_revenue(o):
                    b["revenue"] += _f(o.total_amount)
                    b["rev_orders"] += 1

        rows: List[List[Any]] = []
        t_orders = 0
        t_revenue = 0.0
        for period in sorted(buckets):
            b = buckets[period]
            rev_orders = b["rev_orders"]
            aov = (b["revenue"] / rev_orders) if rev_orders else 0.0
            rows.append([
                period,
                int(b["orders"]),
                round(b["revenue"], 2),
                round(aov, 2),
            ])
            t_orders += int(b["orders"])
            t_revenue += b["revenue"]
        return {
            "columns": columns,
            "rows": rows,
            "summary": {
                "group": group,
                "periods": len(rows),
                "orders": t_orders,
                "revenue": round(t_revenue, 2),
                "avg_order_value": round((t_revenue / t_orders), 2) if t_orders else 0.0,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("REPORTS | sales_report failed: %s", exc)
        return _empty(columns)


# --------------------------------------------------------------------------
# Inventory report
# --------------------------------------------------------------------------

def inventory_report() -> Dict[str, Any]:
    """Per product/variant reserved-vs-committed plus top ordered items.

    Columns: product, variant, reserved, committed, ordered_qty. ``reserved`` and
    ``committed`` are summed from the reservation ledger (status ``reserved`` vs
    ``committed``); ``ordered_qty`` is the total quantity ordered for that
    product across all order items.
    """
    columns = ["product", "variant", "reserved", "committed", "ordered_qty"]
    try:
        # key: (product_retailer_id, variant_id)
        agg: Dict[Any, Dict[str, int]] = defaultdict(
            lambda: {"reserved": 0, "committed": 0, "ordered_qty": 0}
        )
        ordered_by_product: Dict[str, int] = defaultdict(int)
        with session_scope() as session:
            for r in session.query(InventoryReservation).all():
                key = (r.product_retailer_id or "", r.variant_id or "")
                qty = int(r.quantity or 0)
                if r.status == "committed":
                    agg[key]["committed"] += qty
                elif r.status == "reserved":
                    agg[key]["reserved"] += qty
            for it in session.query(OrderItem).all():
                pid = it.product_retailer_id or (it.product_name or "")
                ordered_by_product[pid] += int(it.quantity or 0)
                key = (it.product_retailer_id or "", it.variant_id or "")
                agg[key]["ordered_qty"] += int(it.quantity or 0)

        rows: List[List[Any]] = []
        t_reserved = t_committed = t_ordered = 0
        for (product, variant), v in sorted(agg.items()):
            rows.append([
                product or "—",
                variant or "—",
                v["reserved"],
                v["committed"],
                v["ordered_qty"],
            ])
            t_reserved += v["reserved"]
            t_committed += v["committed"]
            t_ordered += v["ordered_qty"]

        top_ordered = sorted(
            ({"product": p, "ordered_qty": q} for p, q in ordered_by_product.items()),
            key=lambda d: d["ordered_qty"],
            reverse=True,
        )[:10]
        return {
            "columns": columns,
            "rows": rows,
            "summary": {
                "lines": len(rows),
                "reserved": t_reserved,
                "committed": t_committed,
                "ordered_qty": t_ordered,
                "top_ordered": top_ordered,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("REPORTS | inventory_report failed: %s", exc)
        return _empty(columns)


# --------------------------------------------------------------------------
# Customer report
# --------------------------------------------------------------------------

def customer_report(
    date_from: Optional[str] = None, date_to: Optional[str] = None
) -> Dict[str, Any]:
    """Top customers by total spend (orders + revenue) over an optional range.

    Columns: customer, wa_number, orders, revenue, last_order.
    """
    columns = ["customer", "wa_number", "orders", "revenue", "last_order"]
    try:
        agg: Dict[str, Dict[str, Any]] = {}
        with session_scope() as session:
            for o in _orders_in_range(session, date_from, date_to):
                key = o.wa_number or (o.customer_name or "unknown")
                rec = agg.setdefault(
                    key,
                    {"customer": o.customer_name or "", "wa_number": o.wa_number or "",
                     "orders": 0, "revenue": 0.0, "last_order": ""},
                )
                rec["orders"] += 1
                if _is_revenue(o):
                    rec["revenue"] += _f(o.total_amount)
                if o.customer_name and not rec["customer"]:
                    rec["customer"] = o.customer_name
                stamp = o.created_at.date().isoformat() if o.created_at else ""
                if stamp > rec["last_order"]:
                    rec["last_order"] = stamp

        ranked = sorted(agg.values(), key=lambda r: r["revenue"], reverse=True)
        rows = [
            [r["customer"] or r["wa_number"] or "—", r["wa_number"] or "—",
             int(r["orders"]), round(r["revenue"], 2), r["last_order"] or "—"]
            for r in ranked
        ]
        return {
            "columns": columns,
            "rows": rows,
            "summary": {
                "customers": len(rows),
                "revenue": round(sum(r["revenue"] for r in ranked), 2),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("REPORTS | customer_report failed: %s", exc)
        return _empty(columns)


# --------------------------------------------------------------------------
# Product report
# --------------------------------------------------------------------------

def product_report(
    date_from: Optional[str] = None, date_to: Optional[str] = None
) -> Dict[str, Any]:
    """Top products by quantity sold and revenue over an optional range.

    Columns: product, retailer_id, qty, revenue, orders.
    """
    columns = ["product", "retailer_id", "qty", "revenue", "orders"]
    try:
        agg: Dict[str, Dict[str, Any]] = {}
        with session_scope() as session:
            orders = _orders_in_range(session, date_from, date_to)
            order_ids = [o.id for o in orders]
            if not order_ids:
                return {"columns": columns, "rows": [],
                        "summary": {"products": 0, "qty": 0, "revenue": 0.0}}
            items = (
                session.query(OrderItem)
                .filter(OrderItem.order_id.in_(order_ids))
                .all()
            )
            for it in items:
                key = it.product_retailer_id or (it.product_name or "unknown")
                rec = agg.setdefault(
                    key,
                    {"product": it.product_name or "", "retailer_id": it.product_retailer_id or "",
                     "qty": 0, "revenue": 0.0, "orders": set()},
                )
                rec["qty"] += int(it.quantity or 0)
                rec["revenue"] += _f(it.line_total)
                rec["orders"].add(it.order_id)
                if it.product_name and not rec["product"]:
                    rec["product"] = it.product_name

        ranked = sorted(agg.values(), key=lambda r: (r["qty"], r["revenue"]), reverse=True)
        rows = [
            [r["product"] or r["retailer_id"] or "—", r["retailer_id"] or "—",
             int(r["qty"]), round(r["revenue"], 2), len(r["orders"])]
            for r in ranked
        ]
        return {
            "columns": columns,
            "rows": rows,
            "summary": {
                "products": len(rows),
                "qty": sum(int(r["qty"]) for r in ranked),
                "revenue": round(sum(r["revenue"] for r in ranked), 2),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("REPORTS | product_report failed: %s", exc)
        return _empty(columns)


# Registry used by the admin reports blueprint to resolve a report by name.
REPORTS = {
    "gst": gst_report,
    "sales": sales_report,
    "inventory": inventory_report,
    "customer": customer_report,
    "product": product_report,
}


def run_report(name: str, **kwargs: Any) -> Dict[str, Any]:
    """Dispatch to a named report, passing only the kwargs it accepts.

    ``inventory`` takes no date range; the others accept ``date_from`` /
    ``date_to`` (and ``sales`` also ``group``). Unknown names yield an empty
    single-column report rather than raising.
    """
    fn = REPORTS.get((name or "").strip().lower())
    if fn is None:
        logger.warning("REPORTS | Unknown report %r", name)
        return _empty(["report"])
    if fn is inventory_report:
        return inventory_report()
    if fn is sales_report:
        return sales_report(
            date_from=kwargs.get("date_from"),
            date_to=kwargs.get("date_to"),
            group=kwargs.get("group", "day"),
        )
    return fn(date_from=kwargs.get("date_from"), date_to=kwargs.get("date_to"))
