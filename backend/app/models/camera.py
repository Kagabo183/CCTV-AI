"""Camera platform tables. A camera itself is a `video_sources` row with a live kind (see app/cameras).

gateways            a Visionary Gateway on a customer network (secrets stored as hashes only)
camera_credentials  encrypted username/password of a camera (Fernet, never returned by the API)
camera_shares       who else may use a camera, with a role and permissions
stream_sessions     live-view sessions (viewers, relay use, bytes) for cost control and observability
audit_logs          security-relevant actions (credential changes, sharing, gateway enrolment, deletes)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, utcnow


class Gateway(IdMixin, TimestampMixin, Base):
    __tablename__ = "gateways"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | online | offline | revoked
    enrollment_token_hash: Mapped[str | None] = mapped_column(String(128))
    enrollment_expires_at: Mapped[datetime | None]
    secret_hash: Mapped[str | None] = mapped_column(String(128))  # sha256 of the gateway's long-lived secret
    last_heartbeat_at: Mapped[datetime | None]
    version: Mapped[str | None] = mapped_column(String(40))
    gateway_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)  # host, LAN networks, health, last discovery


class CameraCredential(IdMixin, Base):
    __tablename__ = "camera_credentials"

    video_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_sources.id", ondelete="CASCADE"), unique=True, index=True)
    secret: Mapped[str] = mapped_column(Text)  # Fernet token of {"username": ..., "password": ...}
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CameraShare(IdMixin, Base):
    __tablename__ = "camera_shares"
    __table_args__ = (UniqueConstraint("video_source_id", "user_id", name="uq_camera_shares_source_user"),)

    video_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_sources.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # admin | operator | viewer (the owner is video_sources.owner_id)
    permissions: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class StreamSession(IdMixin, Base):
    __tablename__ = "stream_sessions"

    video_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_sources.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    purpose: Mapped[str] = mapped_column(String(16), default="view")  # view | ai | record
    transport: Mapped[str] = mapped_column(String(16), default="webrtc")  # webrtc | hls | rtsp
    relayed: Mapped[bool] = mapped_column(Boolean, default=False)
    stream: Mapped[str | None] = mapped_column(String(16))  # main | sub
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(48))
    target_type: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
