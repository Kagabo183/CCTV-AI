"""Opening a video for analysis.

"Opening" a source = asking the analyzer to prepare it (for Gemini: upload
once). The prepared handle is stored on a VideoSession and reused for every
question until it expires, so we never re-upload per question.

Phase 1 runs preparation as an in-process asyncio task. Moving it to a Redis
backed worker (arq/Celery) later only changes `schedule_prepare`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzers.base import PreparedVideo, VideoAnalyzer
from app.core.errors import AppError, ProviderUnavailable
from app.db.session import get_sessionmaker
from app.models import VideoSession, VideoSourceRecord
from app.video.gateway import VideoGateway

logger = logging.getLogger(__name__)

_locks: dict[uuid.UUID, asyncio.Lock] = {}
_tasks: set[asyncio.Task[None]] = set()


def _lock(session_id: uuid.UUID) -> asyncio.Lock:
    return _locks.setdefault(session_id, asyncio.Lock())


def _prepared_from(session: VideoSession) -> PreparedVideo | None:
    if session.status != "ready" or not session.provider_ref:
        return None
    return PreparedVideo(analyzer=session.analyzer, ref=session.provider_ref, expires_at=_aware(session.provider_ref_expires_at), media_metadata=session.media_metadata)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:  # SQLite drops tzinfo
        return value.replace(tzinfo=UTC)
    return value


class VideoSessionService:
    def __init__(self, db: AsyncSession, gateway: VideoGateway, analyzer: VideoAnalyzer) -> None:
        self.db = db
        self.gateway = gateway
        self.analyzer = analyzer

    async def open(self, source: VideoSourceRecord, user_id: uuid.UUID) -> VideoSession:
        """Return a usable session for this source, creating one if needed."""
        existing = (
            await self.db.execute(
                select(VideoSession)
                .where(VideoSession.video_source_id == source.id, VideoSession.user_id == user_id, VideoSession.analyzer == self.analyzer.name)
                .order_by(VideoSession.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            prepared = _prepared_from(existing)
            if existing.status == "preparing" or (prepared and self.analyzer.is_valid(prepared, datetime.now(UTC))):
                return existing
        session = VideoSession(video_source_id=source.id, user_id=user_id, analyzer=self.analyzer.name, status="preparing")
        self.db.add(session)
        await self.db.commit()
        return session

    async def ensure_ready(self, session_id: uuid.UUID) -> tuple[VideoSession, PreparedVideo]:
        async with _lock(session_id):
            session = await self.db.get(VideoSession, session_id, populate_existing=True)
            if session is None:
                raise AppError("Video session not found", code="session_missing")
            prepared = _prepared_from(session)
            if prepared and self.analyzer.is_valid(prepared, datetime.now(UTC)):
                return session, prepared

            source = await self.db.get(VideoSourceRecord, session.video_source_id)
            assert source is not None
            session.status = "preparing"
            await self.db.commit()
            media = None
            try:
                video_source = self.gateway.build(source.kind, source.uri, source_id=source.id, metadata=source.source_metadata)
                media = await self.gateway.acquire(video_source)
                prepared = await self.analyzer.prepare(media)
            except AppError as exc:
                session.status = "error"
                session.status_message = exc.message
                await self.db.commit()
                raise
            except Exception as exc:
                logger.exception("Preparing video failed")
                session.status = "error"
                session.status_message = "Unexpected error while preparing the video"
                await self.db.commit()
                raise ProviderUnavailable("Could not prepare the video for analysis") from exc
            finally:
                if media is not None:
                    await media.release()  # never keep downloaded footage longer than needed

            session.status = "ready"
            session.status_message = None
            session.provider_ref = prepared.ref
            session.provider_ref_expires_at = prepared.expires_at
            session.media_metadata = prepared.media_metadata
            if duration := prepared.media_metadata.get("duration_seconds"):
                source.source_metadata = {**source.source_metadata, "duration_seconds": duration}
            await self.db.commit()
            return session, prepared

    async def invalidate(self, session: VideoSession) -> None:
        session.status = "expired"
        session.provider_ref = {}
        await self.db.commit()


def schedule_prepare(session_id: uuid.UUID, gateway: VideoGateway, analyzer: VideoAnalyzer) -> None:
    """Warm up a session in the background right after the user opens a video."""

    async def run() -> None:
        async with get_sessionmaker()() as db:
            try:
                await VideoSessionService(db, gateway, analyzer).ensure_ready(session_id)
            except AppError as exc:
                logger.info("Background prepare failed: %s", exc.code)

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
