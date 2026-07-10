"""
admin/analytics.py
-------------------
Read-side query layer for the Admin Dashboard.

Every function returns plain Python dicts/lists (JSON-serialisable) built from
the SQLite datastore in :mod:`admin.db`. There is no business logic or mutation
here — only aggregation and formatting for the UI and the JSON APIs.

All timestamps are stored as UTC ISO-8601 strings, so lexical string comparison
is equivalent to chronological comparison; the date-range helpers below rely on
that property.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from admin.db import get_conn


# --------------------------------------------------------------------------
# Date-range helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def range_bounds(
    period: str = "", start: str = "", end: str = ""
) -> Tuple[Optional[str], Optional[str]]:
    """Translate a named period (or explicit dates) into ISO lower/upper bounds.

    Supported periods: today, yesterday, last7, last30, custom, all (default).
    """
    now = _now()
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        return _iso(start_of_today), None
    if period == "yesterday":
        return _iso(start_of_today - timedelta(days=1)), _iso(start_of_today)
    if period == "last7":
        return _iso(start_of_today - timedelta(days=6)), None
    if period == "last30":
        return _iso(start_of_today - timedelta(days=29)), None
    if period == "custom" or start or end:
        lo = f"{start}T00:00:00+00:00" if start else None
        hi = f"{end}T23:59:59+00:00" if end else None
        return lo, hi
    return None, None


def _apply_range(
    where: List[str], params: List[Any], column: str, lo: Optional[str], hi: Optional[str]
) -> None:
    """Append range predicates to a WHERE clause builder."""
    if lo:
        where.append(f"{column} >= ?")
        params.append(lo)
    if hi:
        where.append(f"{column} < ?")
        params.append(hi)


# --------------------------------------------------------------------------
# Dashboard home
# --------------------------------------------------------------------------

def dashboard_stats() -> Dict[str, int]:
    """Return the headline card metrics for the dashboard home."""
    today_lo, _ = range_bounds("today")
    with get_conn() as conn:
        total_customers = conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
        total_conversations = conn.execute(
            "SELECT COUNT(*) c FROM conversations"
        ).fetchone()["c"]
        todays_messages = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE created_at >= ?", (today_lo,)
        ).fetchone()["c"]
        products_sent = conn.execute(
            "SELECT COUNT(*) c FROM product_sends"
        ).fetchone()["c"]
        ai_replies = conn.execute("SELECT COUNT(*) c FROM ai_history").fetchone()["c"]
        unread = conn.execute(
            "SELECT COALESCE(SUM(unread_count),0) c FROM conversations"
        ).fetchone()["c"]
        orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    return {
        "total_customers": int(total_customers),
        "todays_messages": int(todays_messages),
        "total_conversations": int(total_conversations),
        "products_sent": int(products_sent),
        "ai_replies": int(ai_replies),
        "shopify_orders": int(orders),
        "unread": int(unread),
    }


def daily_messages(days: int = 14) -> Dict[str, List[Any]]:
    """Return message counts per day for the last ``days`` days (chart data)."""
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT substr(created_at,1,10) d, "
            "SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) inbound, "
            "SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) outbound "
            "FROM messages WHERE created_at >= ? GROUP BY d ORDER BY d",
            (_iso(start),),
        ).fetchall()
    by_day = {r["d"]: (int(r["inbound"]), int(r["outbound"])) for r in rows}
    labels: List[str] = []
    inbound: List[int] = []
    outbound: List[int] = []
    for i in range(days):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(day[5:])  # MM-DD
        counts = by_day.get(day, (0, 0))
        inbound.append(counts[0])
        outbound.append(counts[1])
    return {"labels": labels, "inbound": inbound, "outbound": outbound}


def top_customers(limit: int = 5) -> List[Dict[str, Any]]:
    """Return the most active customers by message volume."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.wa_number, c.profile_name, "
            "(SELECT COUNT(*) FROM messages m WHERE m.wa_number = c.wa_number) msgs "
            "FROM conversations c ORDER BY msgs DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "wa_number": r["wa_number"],
            "name": r["profile_name"] or r["wa_number"],
            "messages": int(r["msgs"]),
        }
        for r in rows
    ]


def popular_products(limit: int = 5) -> List[Dict[str, Any]]:
    """Return the most-frequently-shown products."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, price, times_sent FROM products "
            "ORDER BY times_sent DESC, last_sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"title": r["title"], "price": r["price"], "count": int(r["times_sent"])}
        for r in rows
    ]


# --------------------------------------------------------------------------
# Inbox + chat
# --------------------------------------------------------------------------

def inbox(search: str = "", only_unread: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
    """Return conversation summaries for the live inbox, newest first."""
    where: List[str] = []
    params: List[Any] = []
    if search:
        where.append("(wa_number LIKE ? OR profile_name LIKE ? OR last_message LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if only_unread:
        where.append("unread_count > 0")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT wa_number, profile_name, last_message, last_direction, "
            f"message_count, unread_count, status, last_message_at "
            f"FROM conversations {clause} ORDER BY last_message_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def chat_history(wa_number: str, search: str = "") -> Dict[str, Any]:
    """Return the full message timeline for a customer, plus their profile."""
    with get_conn() as conn:
        customer = conn.execute(
            "SELECT * FROM customers WHERE wa_number = ?", (wa_number,)
        ).fetchone()
        convo = conn.execute(
            "SELECT * FROM conversations WHERE wa_number = ?", (wa_number,)
        ).fetchone()
        msg_where = ["wa_number = ?"]
        params: List[Any] = [wa_number]
        if search:
            msg_where.append("text LIKE ?")
            params.append(f"%{search}%")
        messages = conn.execute(
            f"SELECT direction, text, language, intent, latency_ms, created_at "
            f"FROM messages WHERE {' AND '.join(msg_where)} ORDER BY id ASC",
            tuple(params),
        ).fetchall()
        ai_rows = conn.execute(
            "SELECT latency_ms, fallback_used, created_at FROM ai_history "
            "WHERE wa_number = ? ORDER BY id ASC",
            (wa_number,),
        ).fetchall()
    return {
        "wa_number": wa_number,
        "customer": dict(customer) if customer else {"wa_number": wa_number},
        "conversation": dict(convo) if convo else {},
        "messages": [dict(m) for m in messages],
        "ai_meta": [dict(a) for a in ai_rows],
    }


# --------------------------------------------------------------------------
# AI history
# --------------------------------------------------------------------------

def ai_history(
    search: str = "",
    period: str = "",
    start: str = "",
    end: str = "",
    only_fallback: bool = False,
    limit: int = 300,
) -> List[Dict[str, Any]]:
    """Return AI generation records with optional search / date / fallback filters."""
    lo, hi = range_bounds(period, start, end)
    where: List[str] = []
    params: List[Any] = []
    _apply_range(where, params, "created_at", lo, hi)
    if search:
        where.append("(wa_number LIKE ? OR user_message LIKE ? OR response LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if only_fallback:
        where.append("fallback_used = 1")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT wa_number, model, user_message, prompt_context, response, "
            f"latency_ms, fallback_used, error, created_at FROM ai_history "
            f"{clause} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Analytics page
# --------------------------------------------------------------------------

def analytics_summary(period: str = "", start: str = "", end: str = "") -> Dict[str, Any]:
    """Return the analytics-page aggregate metrics for a date range."""
    lo, hi = range_bounds(period, start, end)
    mwhere: List[str] = []
    mparams: List[Any] = []
    _apply_range(mwhere, mparams, "created_at", lo, hi)
    mclause = f"WHERE {' AND '.join(mwhere)}" if mwhere else ""

    with get_conn() as conn:
        msg_count = conn.execute(
            f"SELECT COUNT(*) c FROM messages {mclause}", tuple(mparams)
        ).fetchone()["c"]
        ai_row = conn.execute(
            f"SELECT COUNT(*) c, AVG(latency_ms) avg_ms, "
            f"SUM(fallback_used) fallbacks FROM ai_history {mclause}",
            tuple(mparams),
        ).fetchone()
        products = conn.execute(
            f"SELECT COUNT(*) c FROM product_sends {mclause}", tuple(mparams)
        ).fetchone()["c"]
        orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        active_customers = conn.execute(
            f"SELECT COUNT(DISTINCT wa_number) c FROM messages {mclause}", tuple(mparams)
        ).fetchone()["c"]

    ai_count = int(ai_row["c"] or 0)
    fallbacks = int(ai_row["fallbacks"] or 0)
    avg_ms = float(ai_row["avg_ms"] or 0.0)
    success = ai_count - fallbacks
    accuracy = round((success / ai_count) * 100, 1) if ai_count else 0.0
    conversion = round((orders / active_customers) * 100, 1) if active_customers else 0.0
    return {
        "messages": int(msg_count),
        "ai_replies": ai_count,
        "avg_response_ms": round(avg_ms, 1),
        "ai_accuracy": accuracy,
        "fallbacks": fallbacks,
        "products_sent": int(products),
        "orders_generated": int(orders),
        "active_customers": int(active_customers),
        "conversion_rate": conversion,
        "top_customers": top_customers(10),
        "popular_products": popular_products(10),
        "daily": daily_messages(14),
    }


# --------------------------------------------------------------------------
# Customers + global search
# --------------------------------------------------------------------------

def list_customers(search: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """Return customers with message/product counts for the customers page."""
    where: List[str] = []
    params: List[Any] = []
    if search:
        where.append("(wa_number LIKE ? OR profile_name LIKE ? OR email LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT c.wa_number, c.profile_name, c.language, c.email, c.tags, "
            f"c.first_seen_at, c.last_seen_at, "
            f"(SELECT COUNT(*) FROM messages m WHERE m.wa_number = c.wa_number) msgs "
            f"FROM customers c {clause} ORDER BY c.last_seen_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def customer_detail(wa_number: str) -> Dict[str, Any]:
    """Return a full customer profile: info, counts, products, recent orders."""
    with get_conn() as conn:
        customer = conn.execute(
            "SELECT * FROM customers WHERE wa_number = ?", (wa_number,)
        ).fetchone()
        msg_count = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE wa_number = ?", (wa_number,)
        ).fetchone()["c"]
        products = conn.execute(
            "SELECT title, price, created_at FROM product_sends WHERE wa_number = ? "
            "ORDER BY id DESC LIMIT 50",
            (wa_number,),
        ).fetchall()
        orders = conn.execute(
            "SELECT order_name, financial_status, fulfillment_status, total_price, "
            "currency, looked_up_at FROM orders WHERE wa_number = ? OR phone = ? "
            "ORDER BY id DESC LIMIT 25",
            (wa_number, wa_number),
        ).fetchall()
    return {
        "customer": dict(customer) if customer else {"wa_number": wa_number},
        "message_count": int(msg_count),
        "products_recommended": [dict(p) for p in products],
        "orders": [dict(o) for o in orders],
    }


def global_search(query: str, limit: int = 25) -> Dict[str, List[Dict[str, Any]]]:
    """Search across customers, messages, products and orders in one call."""
    like = f"%{query}%"
    with get_conn() as conn:
        customers = conn.execute(
            "SELECT wa_number, profile_name, email FROM customers "
            "WHERE wa_number LIKE ? OR profile_name LIKE ? OR email LIKE ? LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
        messages = conn.execute(
            "SELECT wa_number, direction, text, created_at FROM messages "
            "WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        products = conn.execute(
            "SELECT title, price, times_sent FROM products WHERE title LIKE ? "
            "ORDER BY times_sent DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        orders = conn.execute(
            "SELECT order_name, customer_name, total_price, currency FROM orders "
            "WHERE order_name LIKE ? OR customer_name LIKE ? OR email LIKE ? "
            "OR phone LIKE ? ORDER BY id DESC LIMIT ?",
            (like, like, like, like, limit),
        ).fetchall()
    return {
        "customers": [dict(r) for r in customers],
        "messages": [dict(r) for r in messages],
        "products": [dict(r) for r in products],
        "orders": [dict(r) for r in orders],
    }
