"""
database/models.py
-------------------
SQLAlchemy ORM models for ME-HAAT Fashion AI Bot v4.0.

This is the Phase-1 core of the v4.0 data model. It intentionally covers the
entities the bot uses today (merchants, OAuth tokens, conversations, customers,
AI logs, cached products, settings). Additional v4.0 models (OrderCache, FAQ,
Knowledge, Sessions) are on the roadmap and can be added here without touching
callers.

All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Merchant(Base):
    """A Shopify store that has installed the app."""

    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    installed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class OAuthToken(Base):
    """A stored Shopify Admin API access token for a merchant."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    access_token: Mapped[str] = mapped_column(Text)  # may be encrypted at rest
    scopes: Mapped[Optional[str]] = mapped_column(Text, default="")
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Customer(Base):
    """A WhatsApp customer the bot has interacted with."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    profile_name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    language: Mapped[Optional[str]] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Conversation(Base):
    """A single conversation turn (user or assistant)."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AILog(Base):
    """An audit record of an AI generation (prompt context + reply)."""

    __tablename__ = "ai_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    user_message: Mapped[str] = mapped_column(Text)  # PII-masked
    reply: Mapped[str] = mapped_column(Text)
    context: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProductCache(Base):
    """A lightweight cache of verified products (for analytics / fast reads)."""

    __tablename__ = "product_cache"
    __table_args__ = (UniqueConstraint("shop_domain", "product_id", name="uq_shop_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_domain: Mapped[str] = mapped_column(String(255), index=True)
    product_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(512))
    price: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    currency: Mapped[Optional[str]] = mapped_column(String(8), default="INR")
    product_type: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Settings(Base):
    """Simple key/value settings store (per shop, optional)."""

    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("shop_domain", "key", name="uq_shop_setting"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_domain: Mapped[Optional[str]] = mapped_column(String(255), index=True, default=None)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[Optional[str]] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ==========================================================================
# v6.0 Enterprise Commerce models
# --------------------------------------------------------------------------
# These back the WhatsApp Commerce platform: orders placed from the WhatsApp
# catalog, their line items, payments, tracking history, generated invoices,
# outbound notifications, and an audit trail. They are created on startup by
# ``bootstrap_commerce()`` regardless of USE_DATABASE (commerce needs durable
# persistence) and work identically on SQLite and PostgreSQL.
# ==========================================================================

# Canonical status values (kept as plain strings for cross-DB portability).
ORDER_STATUSES = (
    "received", "confirmed", "packed", "shipped",
    "out_for_delivery", "delivered", "cancelled", "refunded",
)
PAYMENT_STATUSES = ("pending", "paid", "failed", "refunded", "expired")
FULFILLMENT_STATUSES = ("unfulfilled", "packed", "shipped", "out_for_delivery", "fulfilled")


class Order(Base):
    """A customer order placed via the WhatsApp catalog (or the admin)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Human-facing internal number, e.g. MH-2026-000001.
    order_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    wa_number: Mapped[str] = mapped_column(String(32), index=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    language: Mapped[Optional[str]] = mapped_column(String(16), default=None)

    # WhatsApp / Meta commerce references.
    wa_order_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, default=None)
    catalog_id: Mapped[Optional[str]] = mapped_column(String(128), default=None)

    currency: Mapped[str] = mapped_column(String(8), default="INR")
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    shipping: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    tax: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)

    status: Mapped[str] = mapped_column(String(24), default="received", index=True)
    payment_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    fulfillment_status: Mapped[str] = mapped_column(String(24), default="unfulfilled")

    # Shopify linkage.
    shopify_draft_order_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    shopify_order_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    checkout_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    invoice_url: Mapped[Optional[str]] = mapped_column(Text, default=None)

    # Shipping / geo (used for analytics by state/city).
    courier: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    tracking_number: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    city: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    state: Mapped[Optional[str]] = mapped_column(String(128), default=None)

    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class OrderItem(Base):
    """A single line item within an :class:`Order`."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    product_retailer_id: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    product_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    variant_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    product_name: Mapped[Optional[str]] = mapped_column(String(512), default=None)
    variant: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)


class Payment(Base):
    """A payment attempt/record against an order."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, default=None)
    provider_link_id: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    payment_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    raw: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Tracking(Base):
    """An append-only tracking/status event for an order."""

    __tablename__ = "tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24))
    courier: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    tracking_number: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    location: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    note: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class Invoice(Base):
    """A generated PDF invoice for an order."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text, default=None)
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NotificationLog(Base):
    """An outbound customer/admin notification (audit + retry surface)."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True, default=None
    )
    wa_number: Mapped[Optional[str]] = mapped_column(String(32), index=True, default=None)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    audience: Mapped[str] = mapped_column(String(16), default="customer")  # customer | admin
    channel: Mapped[str] = mapped_column(String(16), default="whatsapp")
    status: Mapped[str] = mapped_column(String(16), default="sent")
    body: Mapped[Optional[str]] = mapped_column(Text, default=None)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class AuditLog(Base):
    """An audit trail of state-changing actions (admin + system)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    detail: Mapped[Optional[str]] = mapped_column(Text, default=None)
    ip: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class AnalyticsSnapshot(Base):
    """A cached daily analytics rollup (optional; computed on demand too)."""

    __tablename__ = "analytics"
    __table_args__ = (UniqueConstraint("day", "metric", name="uq_analytics_day_metric"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    metric: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Counter(Base):
    """A named monotonic counter used to mint sequential order numbers."""

    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
