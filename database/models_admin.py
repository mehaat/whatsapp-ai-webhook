"""
database/models_admin.py
------------------------
ORM models for the previously "raw-SQLite-only" subsystems:

    * Admin Dashboard datastore  (users, dash_customers, dash_conversations,
      messages, ai_history, products, product_sends, dash_orders)
    * Shopify OAuth store        (oauth_states, shop_tokens)

These tables were historically created with hand-written ``CREATE TABLE`` DDL
and accessed via ``sqlite3``. They are now first-class ORM models on the ONE
shared ``Base``, so:

    * ``Base.metadata.create_all`` builds them on **any** backend (SQLite or
      Postgres) with portable column types, and
    * Alembic ``--autogenerate`` sees and versions them alongside the commerce
      schema.

The column names, types, nullability, defaults and indexes are chosen to be a
**faithful, byte-compatible mirror** of the legacy DDL so the existing raw SQL
in ``admin/tracker.py``, ``admin/analytics.py`` and ``admin/routes.py`` keeps
working verbatim through the portable compatibility shim.

Design note — timestamps as TEXT:
    The dashboard stores timestamps as ISO-8601 **strings** and queries them
    with ``substr(created_at,1,10)``. To preserve 100% behavioural parity we
    keep those columns as ``String`` (portable to Postgres ``VARCHAR``) rather
    than converting to ``DateTime`` — the latter would silently change every
    dashboard aggregation. OAuth timestamps are UNIX ``REAL``/float values and
    are modelled as ``Float``.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from database.db import Base


# --------------------------------------------------------------------------- #
# Admin Dashboard datastore
# --------------------------------------------------------------------------- #
class DashUser(Base):
    """Admin dashboard login user (legacy raw table name: ``users``)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(Text)
    role: Mapped[Optional[str]] = mapped_column(
        String(50), server_default=text("'admin'")
    )
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_login_at: Mapped[Optional[str]] = mapped_column(String(64))


class DashCustomer(Base):
    """Per-customer dashboard record (legacy: ``dash_customers``)."""

    __tablename__ = "dash_customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    profile_name: Mapped[Optional[str]] = mapped_column(String(255))
    language: Mapped[Optional[str]] = mapped_column(String(16))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    tags: Mapped[Optional[str]] = mapped_column(Text)
    first_seen_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(64), nullable=False)


class DashConversation(Base):
    """Conversation summary row powering the inbox (legacy: ``dash_conversations``)."""

    __tablename__ = "dash_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    profile_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_message: Mapped[Optional[str]] = mapped_column(Text)
    last_direction: Mapped[Optional[str]] = mapped_column(String(8))
    message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    unread_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[Optional[str]] = mapped_column(String(16), server_default=text("'open'"))
    started_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_message_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_conversations_last", "last_message_at"),
    )


class DashMessage(Base):
    """Individual inbound/outbound message log (legacy: ``messages``)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # 'in' | 'out'
    text: Mapped[Optional[str]] = mapped_column(Text)
    language: Mapped[Optional[str]] = mapped_column(String(16))
    intent: Mapped[Optional[str]] = mapped_column(String(64))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_messages_wa", "wa_number"),
        Index("idx_messages_created", "created_at"),
    )


class DashAIHistory(Base):
    """Per-generation Gemini audit row (legacy: ``ai_history``)."""

    __tablename__ = "ai_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128))
    user_message: Mapped[Optional[str]] = mapped_column(Text)  # PII-masked
    prompt_context: Mapped[Optional[str]] = mapped_column(Text)
    response: Mapped[Optional[str]] = mapped_column(Text)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    fallback_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_ai_wa", "wa_number"),
        Index("idx_ai_created", "created_at"),
    )


class DashProduct(Base):
    """Aggregated product popularity row (legacy: ``products``)."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_ref: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[Optional[str]] = mapped_column(String(64))
    currency: Mapped[Optional[str]] = mapped_column(String(16))
    times_sent: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_sent_at: Mapped[Optional[str]] = mapped_column(String(64))


class DashProductSend(Base):
    """Individual product-send event (legacy: ``product_sends``)."""

    __tablename__ = "product_sends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[Optional[str]] = mapped_column(Text)
    title: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_product_sends_created", "created_at"),
    )


class DashOrder(Base):
    """Cached order-lookup row for fast dashboard reads (legacy: ``dash_orders``)."""

    __tablename__ = "dash_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_name: Mapped[Optional[str]] = mapped_column(String(64))
    wa_number: Mapped[Optional[str]] = mapped_column(String(64))
    customer_name: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    financial_status: Mapped[Optional[str]] = mapped_column(String(64))
    fulfillment_status: Mapped[Optional[str]] = mapped_column(String(64))
    total_price: Mapped[Optional[str]] = mapped_column(String(64))
    currency: Mapped[Optional[str]] = mapped_column(String(16))
    tracking: Mapped[Optional[str]] = mapped_column(Text)
    looked_up_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_orders_name", "order_name"),
    )


# --------------------------------------------------------------------------- #
# Shopify OAuth store
# --------------------------------------------------------------------------- #
class OAuthState(Base):
    """One-time CSRF/replay ``state`` for the Shopify OAuth flow (``oauth_states``).

    Shared across every Gunicorn worker so the worker that issues a state and
    the worker that consumes it on the callback read the same durable table.
    Timestamps are UNIX floats (``time.time()``), matching the legacy schema.
    """

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(255), primary_key=True)
    shop: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("idx_oauth_states_expires", "expires_at"),
    )


class ShopToken(Base):
    """Per-shop Shopify access token (``shop_tokens``).

    ``shop`` is the primary key so installs are idempotent. ``installed_at``
    preserves installation order for :meth:`TokenStore.get_default_shop`
    ("first installed" wins). The token value may be Fernet-encrypted at rest
    when ``TOKEN_ENCRYPTION_KEY`` is configured.
    """

    __tablename__ = "shop_tokens"

    shop: Mapped[str] = mapped_column(String(255), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    installed_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("idx_shop_tokens_installed", "installed_at"),
    )


__all__ = [
    "DashUser",
    "DashCustomer",
    "DashConversation",
    "DashMessage",
    "DashAIHistory",
    "DashProduct",
    "DashProductSend",
    "DashOrder",
    "OAuthState",
    "ShopToken",
]
