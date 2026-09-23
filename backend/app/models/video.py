from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, utcnow


class VideoSourceRecord(IdMixin, TimestampMixin, Base):
    """A registered video source. `kind` selects the VideoSource implementation.

    kind: url | local (Phase 1) and rtsp | onvif | nvr (future). For live
    cameras `uri` will hold the stream address and credentials will move to a
    separate secret store rather than this row.
    """

    __tablename__ = "video_sources"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))  # e.g. "irembo" - used to resolve spoken references
    kind: Mapped[str] = mapped_column(String(16))
    uri: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | ready | error
    status_message: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    last_validated_at: Mapped[datetime | None]


class VideoSession(IdMixin, TimestampMixin, Base):
    """A source opened for analysis by a specific analyzer.

    Holds the analyzer-side handle (e.g. a Gemini file URI) so the video is
    uploaded once and reused across questions until it expires.
    """

    __tablename__ = "video_sessions"

    video_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_sources.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    analyzer: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="preparing")  # preparing | ready | error | expired
    status_message: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provider_ref_expires_at: Mapped[datetime | None]
    media_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)


class VideoEvent(IdMixin, Base):
    """Something that happened in a video.

    Phase 1 events come from the analyzer's answers. The local engine will
    later write events continuously; `detector` records who produced it.
    Times are seconds from the start of the media (start_time/end_time), and
    `occurred_at` is the wall-clock time for live sources.
    """

    __tablename__ = "video_events"
    __table_args__ = (Index("ix_video_events_source_type", "video_source_id", "event_type"),)

    video_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_sources.id", ondelete="CASCADE"), index=True)
    video_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("video_sessions.id", ondelete="SET NULL"))
    analysis_request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("analysis_requests.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text)
    start_time: Mapped[float | None] = mapped_column(Float)
    end_time: Mapped[float | None] = mapped_column(Float)
    occurred_at: Mapped[datetime | None]
    confidence: Mapped[float | None] = mapped_column(Float)
    detector: Mapped[str] = mapped_column(String(32))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
