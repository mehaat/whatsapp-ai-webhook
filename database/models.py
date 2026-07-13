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

    # v7.0 soft delete: non-null hides the order from listings without losing data.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    # v8.0 multi-tenant: which store/brand this order belongs to (null = default).
    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, default=None)

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
    # v8.0 tamper-evident hash chain (each row hashes its content + prev row hash).
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    row_hash: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)
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


# ==========================================================================
# v6.1 models — RBAC, background jobs, inventory reservation, CRM
# ==========================================================================

# Role hierarchy (higher index = more privilege). Used for role_required checks.
ADMIN_ROLES = ("viewer", "staff", "manager", "admin", "owner")


class AdminUser(Base):
    """A dashboard user with a role (multi-user RBAC, v6.1).

    The environment ADMIN_USERNAME/ADMIN_PASSWORD continues to work as a
    built-in ``owner`` superuser; these rows are additional named users.
    """

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="staff", index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    email: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)


class Job(Base):
    """A durable background job (v6.1 queue).

    Persisted for auditability and crash-recovery; a worker pool executes them
    asynchronously so the webhook request returns quickly.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, default=None)  # JSON
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    result: Mapped[Optional[str]] = mapped_column(Text, default=None)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class InventoryReservation(Base):
    """A held quantity against an order line (v6.1 reservation ledger)."""

    __tablename__ = "inventory_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    product_retailer_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, default=None)
    variant_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="reserved", index=True)
    synced_to_shopify: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CustomerNote(Base):
    """A free-text CRM note attached to a customer (v6.1)."""

    __tablename__ = "customer_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(32), index=True)
    author: Mapped[str] = mapped_column(String(128), default="admin")
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class CrmProfile(Base):
    """CRM enrichment for a customer: tags, segment, cached lifetime value."""

    __tablename__ = "crm_profiles"

    wa_number: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    tags: Mapped[Optional[str]] = mapped_column(Text, default="")  # comma-separated
    segment: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    lifetime_value: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ==========================================================================
# v7.0 Enterprise models — commerce depth, fulfilment, admin/ops
# ==========================================================================


class Coupon(Base):
    """A discount coupon (percent or flat) with usage limits + validity window."""

    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="percent")  # percent | flat
    value: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    min_order: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    max_discount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), default=None)
    usage_limit: Mapped[Optional[int]] = mapped_column(Integer, default=None)  # global cap
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    per_customer_limit: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CouponRedemption(Base):
    """A record of a coupon being applied to an order."""

    __tablename__ = "coupon_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("coupons.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True, default=None
    )
    wa_number: Mapped[Optional[str]] = mapped_column(String(32), index=True, default=None)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GiftCard(Base):
    """A stored-value gift card with a running balance."""

    __tablename__ = "gift_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    initial_balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    issued_to: Mapped[Optional[str]] = mapped_column(String(32), default=None)  # wa_number
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GiftCardTxn(Base):
    """A gift-card ledger entry (issue / redeem / refund)."""

    __tablename__ = "gift_card_txns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gift_card_id: Mapped[int] = mapped_column(
        ForeignKey("gift_cards.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(String(16), default="redeem")  # issue|redeem|refund
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Bundle(Base):
    """A bundle/combo product: several catalog items sold at a bundle price."""

    __tablename__ = "bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    sku: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True, default=None)
    price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    items: Mapped[Optional[str]] = mapped_column(Text, default="[]")  # JSON [{retailer_id, qty}]
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WishlistItem(Base):
    """A product a customer saved for later."""

    __tablename__ = "wishlist_items"
    __table_args__ = (UniqueConstraint("wa_number", "product_retailer_id", name="uq_wishlist"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(32), index=True)
    product_retailer_id: Mapped[str] = mapped_column(String(128))
    product_name: Mapped[Optional[str]] = mapped_column(String(512), default=None)
    price: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Cart(Base):
    """A customer's working cart; used for abandoned-cart recovery."""

    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|abandoned|converted
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    items: Mapped[Optional[str]] = mapped_column(Text, default="[]")  # JSON
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), default=None
    )
    recovery_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True
    )


class ReturnRequest(Base):
    """A return / refund / exchange request against an order."""

    __tablename__ = "return_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rma_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    wa_number: Mapped[Optional[str]] = mapped_column(String(32), index=True, default=None)
    kind: Mapped[str] = mapped_column(String(16), default="return")  # return|refund|exchange
    reason: Mapped[Optional[str]] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    refund_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    resolution: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Shipment(Base):
    """A shipment created with a courier for an order."""

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="manual")
    awb: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    courier_name: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    label_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    tracking_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    provider_shipment_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    pickup_scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    raw: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class SupportTicket(Base):
    """A customer support ticket."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    wa_number: Mapped[Optional[str]] = mapped_column(String(32), index=True, default=None)
    subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|pending|resolved|closed
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # low|normal|high|urgent
    assigned_to: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TicketMessage(Base):
    """A message within a support ticket thread."""

    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True
    )
    author: Mapped[str] = mapped_column(String(128), default="customer")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LoginEvent(Base):
    """An admin login attempt (success/failure) for the login-history view."""

    __tablename__ = "login_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


# ==========================================================================
# v8.0 Enterprise-scale models — multi-tenant, developer portal, compliance
# ==========================================================================


class Tenant(Base):
    """A store/brand tenant for multi-store / multi-tenant deployments.

    A tenant is resolved from the inbound WhatsApp phone_number_id, the Shopify
    shop domain, an ``X-Tenant`` API header, or the request host. When
    MULTI_TENANT_ENABLED is false everything runs under an implicit default
    tenant, so single-store deployments are unaffected.
    """

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    shopify_domain: Mapped[Optional[str]] = mapped_column(String(255), index=True, default=None)
    whatsapp_phone_number_id: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, default=None
    )
    catalog_id: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    host: Mapped[Optional[str]] = mapped_column(String(255), index=True, default=None)
    config: Mapped[Optional[str]] = mapped_column(Text, default="{}")  # JSON overrides
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ApiKey(Base):
    """A developer API key (hashed at rest) with scopes + a rate limit."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # public id
    key_hash: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    tenant_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), index=True, default=None
    )
    scopes: Mapped[Optional[str]] = mapped_column(Text, default="read")  # csv
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=120)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PiiAccessLog(Base):
    """A record of access to a customer's personal data (compliance)."""

    __tablename__ = "pii_access_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    subject_wa_number: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(64))  # view|export|erase
    ip: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class DataRequest(Base):
    """A GDPR/DPDP data-subject request (export or erasure)."""

    __tablename__ = "data_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # export | erasure
    subject_wa_number: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|completed|failed
    requested_by: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    result_path: Mapped[Optional[str]] = mapped_column(Text, default=None)
    detail: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
