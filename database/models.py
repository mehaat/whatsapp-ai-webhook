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

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
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
