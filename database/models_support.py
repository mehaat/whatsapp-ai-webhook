"""
database/models_support.py
--------------------------
ORM models for the v10.2 real-time WhatsApp Support Console.

These are **new, additive** tables — no existing table is modified. They are
registered on the ONE shared ``Base`` (imported at the end of
``database.models``) so a single ``create_all`` / Alembic migration covers them
on both SQLite and PostgreSQL.

Tables
    conversation_settings     per-conversation AI toggle + status
    admin_messages            messages sent BY an admin from the console
    conversation_assignments  which admin owns a conversation
    internal_notes            admin-only notes (never shown to the customer)
    message_status            WhatsApp delivery/read receipts (wamid -> status)

Timestamps are stored as ISO-8601 **strings** (`String`) to match the existing
dashboard tables (`messages.created_at`, etc.), so the console can merge admin
messages with the existing `messages` timeline by a simple lexicographic sort.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database.db import Base


class ConversationSettings(Base):
    """Per-conversation console settings: the AI on/off toggle and status.

    ``ai_enabled=False`` puts the conversation in **Manual Mode**: the webhook
    handler stops the bot from auto-replying and the admin drives the chat.
    """

    __tablename__ = "conversation_settings"

    wa_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)  # open|pending|closed
    updated_by: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AdminMessage(Base):
    """A message sent by an admin from the console (text / image / doc / voice).

    Recorded separately from the bot/customer ``messages`` table so we keep rich
    metadata (which admin, media id, WhatsApp message id, delivery status). The
    console renders a merged timeline of ``messages`` + ``admin_messages``.
    """

    __tablename__ = "admin_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), nullable=False)
    admin_user: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), default="out", nullable=False)
    msg_type: Mapped[str] = mapped_column(String(16), default="text", nullable=False)  # text|image|document|audio
    body: Mapped[Optional[str]] = mapped_column(Text)          # text or media caption
    media_id: Mapped[Optional[str]] = mapped_column(String(255))  # Meta media id
    media_url: Mapped[Optional[str]] = mapped_column(Text)     # optional public url
    filename: Mapped[Optional[str]] = mapped_column(String(255))
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    wa_message_id: Mapped[Optional[str]] = mapped_column(String(128))  # wamid
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)  # queued|sent|delivered|read|failed
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_admin_messages_wa", "wa_number"),
        Index("idx_admin_messages_created", "created_at"),
        Index("idx_admin_messages_wamid", "wa_message_id"),
    )


class ConversationAssignment(Base):
    """The admin currently responsible for a conversation (one active per wa)."""

    __tablename__ = "conversation_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(128))
    assigned_by: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class InternalNote(Base):
    """Admin-only note attached to a conversation. Never sent to the customer."""

    __tablename__ = "internal_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_number: Mapped[str] = mapped_column(String(64), nullable=False)
    admin_user: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_internal_notes_wa", "wa_number"),
    )


class MessageStatus(Base):
    """A WhatsApp delivery/read receipt for an outbound message (from webhooks)."""

    __tablename__ = "message_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    wa_number: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sent|delivered|read|failed
    timestamp: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_message_status_wamid", "wa_message_id"),
        Index("idx_message_status_wa", "wa_number"),
    )


__all__ = [
    "ConversationSettings",
    "AdminMessage",
    "ConversationAssignment",
    "InternalNote",
    "MessageStatus",
]
