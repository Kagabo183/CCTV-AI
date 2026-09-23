"""Local perception results: runs, tracks and per-second scene snapshots.

Events from the local engine go to `video_events` (with evidence_level="rule"),
alongside Gemini-derived events (evidence_level="model_interpretation").
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, utcnow


class VisionRun(IdMixin, Base):
    """One pass of detector -> tracker -> event engine over a source."""

    __tablename__ = "vision_runs"

    video_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_sources.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | completed | failed
    detector: Mapped[str] = mapped_column(String(32))
    weights: Mapped[str] = mapped_column(String(100))
    tracker: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)  # scene config + sampling used
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None]


class ObjectTrack(IdMixin, Base):
    """A tracked object (one track id) within a run."""

    __tablename__ = "object_tracks"
    __table_args__ = (Index("ix_object_tracks_run_class", "vision_run_id", "object_class"),)

    vision_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vision_runs.id", ondelete="CASCADE"), index=True)
    video_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_sources.id", ondelete="CASCADE"))
    track_id: Mapped[int] = mapped_column(Integer)
    object_class: Mapped[str] = mapped_column(String(32))
    first_seen: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)
    frames: Mapped[int] = mapped_column(Integer)
    mean_confidence: Mapped[float] = mapped_column(Float)
    max_confidence: Mapped[float] = mapped_column(Float)
    track_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)  # first/last bbox, class votes


class SceneSnapshot(IdMixin, Base):
    """What was in view at a moment (sampled once per second)."""

    __tablename__ = "scene_snapshots"
    __table_args__ = (Index("ix_scene_snapshots_run_time", "vision_run_id", "timestamp"),)

    vision_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vision_runs.id", ondelete="CASCADE"))
    timestamp: Mapped[float] = mapped_column(Float)
    counts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    track_ids: Mapped[list[Any]] = mapped_column(default=list)
