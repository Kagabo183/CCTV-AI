from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, utcnow


class Conversation(IdMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    video_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("video_sources.id", ondelete="SET NULL"), index=True)
    video_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("video_sessions.id", ondelete="SET NULL"))
    title: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(8), default="rw")
    # Rolling context carried between turns: entities being discussed, focus
    # timestamps, last referenced events. See conversation/context.py.
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ConversationMessage(IdMixin, Base):
    __tablename__ = "conversation_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8))
    input_mode: Mapped[str | None] = mapped_column(String(8))  # text | voice
    video_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("video_sources.id", ondelete="SET NULL"))
    analysis_request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("analysis_requests.id", ondelete="SET NULL"))
    confidence: Mapped[float | None] = mapped_column(Float)
    timestamps: Mapped[list[Any]] = mapped_column(default=list)
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    message_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
