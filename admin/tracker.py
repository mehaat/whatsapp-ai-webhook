"""
admin/tracker.py
-----------------
Event recorder that feeds the Admin Dashboard with **real** data captured from
live bot traffic.

Every public function here is *best-effort* and fully exception-guarded: a
tracking failure can never propagate into the WhatsApp / Shopify / Gemini reply
paths. This is what makes the dashboard a safe, additive module — the bot keeps
working exactly as before even if the dashboard database is unavailable.

The application calls these hooks from ``app.py`` at the points where messages
are received, replies are sent, products are shown and AI responses are
generated. All writes are idempotent-friendly upserts against the SQLite store
defined in :mod:`admin.db`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence

from admin.db import get_conn
from utils.logging import logger
from utils.security import mask_pii

# Sentinels used internally by the bot that must never be recorded as messages.
_SENTINELS = {"__RATE_LIMITED__", "__UNSUPPORTED_TYPE__"}


def _now_iso() -> str:
    """Return a UTC ISO-8601 timestamp (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dev_mode() -> bool:
    """True in development so tracking failures show full tracebacks."""
    try:
        from config import config

        return bool(getattr(config, "dev_mode", False))
    except Exception:  # noqa: BLE001
        return False


def _safe(fn):
    """Decorator: guard tracking so it never breaks the bot — but NEVER silently.

    v10.1: tracking failures are always surfaced. In development they log a full
    traceback (``logger.exception``); in production they log at ERROR level with
    the failing function + reason. Database failures are no longer hidden at
    DEBUG level (the historical cause of the dashboard silently not updating).
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - tracking must not break the reply path
            if _dev_mode():
                logger.exception("ADMIN | tracker.%s FAILED", fn.__name__)
            else:
                logger.error("ADMIN | tracker.%s failed (db issue?): %s", fn.__name__, exc)
            return None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _touch_conversation(
    conn,
    wa_number: str,
    *,
    profile_name: Optional[str],
    last_message: Optional[str],
    direction: str,
    now: str,
    inbound: bool,
) -> None:
    """Upsert the per-customer conversation summary row used by the inbox."""
    row = conn.execute(
        "SELECT id, message_count, unread_count FROM dash_conversations WHERE wa_number = ?",
        (wa_number,),
    ).fetchone()
    snippet = (last_message or "")[:280]
    if row is None:
        conn.execute(
            "INSERT INTO dash_conversations (wa_number, profile_name, last_message, "
            "last_direction, message_count, unread_count, status, started_at, "
            "last_message_at) VALUES (?, ?, ?, ?, 1, ?, 'open', ?, ?)",
            (wa_number, profile_name, snippet, direction, 1 if inbound else 0, now, now),
        )
        return
    unread = int(row["unread_count"] or 0) + (1 if inbound else 0)
    if not inbound:
        # Bot replied; the thread is "handled" from the operator's perspective.
        unread = 0 if direction == "out" else unread
    conn.execute(
        "UPDATE dash_conversations SET profile_name = COALESCE(?, profile_name), "
        "last_message = ?, last_direction = ?, message_count = message_count + 1, "
        "unread_count = ?, last_message_at = ? WHERE id = ?",
        (profile_name or None, snippet, direction, unread, now, row["id"]),
    )


@_safe
def upsert_customer(
    wa_number: str,
    profile_name: Optional[str] = None,
    language: Optional[str] = None,
    email: Optional[str] = None,
) -> None:
    """Create or update a customer record."""
    if not wa_number:
        return
    now = _now_iso()
    with get_conn(write=True) as conn:
        row = conn.execute(
            "SELECT id FROM dash_customers WHERE wa_number = ?", (wa_number,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO dash_customers (wa_number, profile_name, language, email, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (wa_number, profile_name, language, email, now, now),
            )
        else:
            conn.execute(
                "UPDATE dash_customers SET profile_name = COALESCE(?, profile_name), "
                "language = COALESCE(?, language), email = COALESCE(?, email), "
                "last_seen_at = ? WHERE id = ?",
                (profile_name or None, language or None, email or None, now, row["id"]),
            )


@_safe
def record_inbound(
    wa_number: str, text: str, profile_name: str = "", language: Optional[str] = None
) -> None:
    """Record an inbound customer message and bump the conversation summary."""
    if not wa_number or text in _SENTINELS:
        return
    now = _now_iso()
    with get_conn(write=True) as conn:
        conn.execute(
            "INSERT INTO messages (wa_number, direction, text, language, created_at) "
            "VALUES (?, 'in', ?, ?, ?)",
            (wa_number, text, language, now),
        )
        _touch_conversation(
            conn,
            wa_number,
            profile_name=profile_name or None,
            last_message=text,
            direction="in",
            now=now,
            inbound=True,
        )
    upsert_customer(wa_number, profile_name or None, language)


@_safe
def record_outbound(
    wa_number: str,
    text: str,
    *,
    intent: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> None:
    """Record an outbound bot reply and mark the conversation as handled."""
    if not wa_number:
        return
    now = _now_iso()
    with get_conn(write=True) as conn:
        conn.execute(
            "INSERT INTO messages (wa_number, direction, text, intent, latency_ms, "
            "created_at) VALUES (?, 'out', ?, ?, ?, ?)",
            (wa_number, text, intent, latency_ms, now),
        )
        _touch_conversation(
            conn,
            wa_number,
            profile_name=None,
            last_message=text,
            direction="out",
            now=now,
            inbound=False,
        )


@_safe
def record_ai(
    wa_number: str,
    user_message: str,
    response: str,
    *,
    model: Optional[str] = None,
    prompt_context: Optional[str] = None,
    latency_ms: Optional[int] = None,
    fallback_used: bool = False,
    error: Optional[str] = None,
) -> None:
    """Record a single Gemini generation for the AI-history view."""
    if not wa_number:
        return
    now = _now_iso()
    with get_conn(write=True) as conn:
        conn.execute(
            "INSERT INTO ai_history (wa_number, model, user_message, prompt_context, "
            "response, latency_ms, fallback_used, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                wa_number,
                model,
                mask_pii(user_message or ""),
                (prompt_context or "")[:8000] or None,
                response,
                latency_ms,
                1 if fallback_used else 0,
                error,
                now,
            ),
        )


@_safe
def record_products_sent(
    wa_number: str, query: str, products: Sequence[dict]
) -> None:
    """Record the products shown to a customer (for analytics + popularity)."""
    if not wa_number or not products:
        return
    now = _now_iso()
    with get_conn(write=True) as conn:
        for product in products:
            title = str(product.get("title") or product.get("name") or "").strip()
            if not title:
                continue
            price = str(product.get("price") or product.get("formatted_price") or "")
            currency = str(product.get("currency") or "")
            ref = str(product.get("id") or product.get("product_id") or title)[:255]
            conn.execute(
                "INSERT INTO product_sends (wa_number, query, title, price, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (wa_number, query, title, price, now),
            )
            existing = conn.execute(
                "SELECT id, times_sent FROM products WHERE product_ref = ?", (ref,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO products (product_ref, title, price, currency, "
                    "times_sent, last_sent_at) VALUES (?, ?, ?, ?, 1, ?)",
                    (ref, title, price, currency, now),
                )
            else:
                conn.execute(
                    "UPDATE products SET times_sent = times_sent + 1, "
                    "last_sent_at = ?, price = ? WHERE id = ?",
                    (now, price, existing["id"]),
                )


@_safe
def record_order_lookup(order: dict) -> None:
    """Record an order that was looked up (audit + fast dashboard reads)."""
    if not order:
        return
    now = _now_iso()
    with get_conn(write=True) as conn:
        conn.execute(
            "INSERT INTO dash_orders (order_name, wa_number, customer_name, email, phone, "
            "financial_status, fulfillment_status, total_price, currency, tracking, "
            "looked_up_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order.get("order_name"),
                order.get("wa_number"),
                order.get("customer_name"),
                order.get("email"),
                order.get("phone"),
                order.get("financial_status"),
                order.get("fulfillment_status"),
                order.get("total_price"),
                order.get("currency"),
                order.get("tracking"),
                now,
            ),
        )


@_safe
def mark_read(wa_number: str) -> None:
    """Clear the unread counter for a conversation (called when opened)."""
    with get_conn(write=True) as conn:
        conn.execute(
            "UPDATE dash_conversations SET unread_count = 0 WHERE wa_number = ?",
            (wa_number,),
        )


def record_login(username: str) -> None:
    """Update the ``users.last_login_at`` timestamp (best-effort)."""
    try:
        now = _now_iso()
        with get_conn(write=True) as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE username = ?",
                (now, username),
            )
    except Exception as exc:  # noqa: BLE001
        if _dev_mode():
            logger.exception("ADMIN | record_login FAILED")
        else:
            logger.error("ADMIN | record_login failed: %s", exc)


__all__: List[str] = [
    "upsert_customer",
    "record_inbound",
    "record_outbound",
    "record_ai",
    "record_products_sent",
    "record_order_lookup",
    "mark_read",
    "record_login",
]
