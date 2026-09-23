from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.analyzers.base import PreparedVideo
from app.api.deps import Analyzer, CurrentUser, DbSession, Gateway, owned_source
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError, ValidationFailed
from app.models import VideoEvent, VideoSession, VideoSourceRecord
from app.schemas.api import PlaybackOut, VideoEventOut, VideoSessionOut, VideoSourceIn, VideoSourceOut, VideoSourceUpdate
from app.services.video_sessions import VideoSessionService, schedule_prepare
from app.video.gateway import VideoGateway
from app.video.sources.local_source import LocalFileVideoSource
from app.video.sources.upload_source import UploadedVideoSource, store_upload

router = APIRouter(prefix="/video-sources", tags=["video sources"])
OwnedSource = Annotated[VideoSourceRecord, Depends(owned_source)]


def serialize_source(source: VideoSourceRecord, gw: VideoGateway) -> VideoSourceOut:
    playback = None
    try:
        info = gw.build(source.kind, source.uri, source_id=source.id, metadata=source.source_metadata).playback()
        playback = PlaybackOut(type=info.type, url=info.url)
    except AppError:
        pass  # e.g. local sources after they were disabled
    return VideoSourceOut(
        id=source.id,
        name=source.name,
        location=source.location,
        kind=source.kind,
        uri=source.uri,
        status=source.status,
        status_message=source.status_message,
        metadata=source.source_metadata,
        playback=playback,
        created_at=source.created_at,
    )


@router.get("", response_model=list[VideoSourceOut])
async def list_sources(user: CurrentUser, db: DbSession, gw: Gateway) -> list[VideoSourceOut]:
    rows = await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.owner_id == user.id).order_by(VideoSourceRecord.created_at.desc()))
    return [serialize_source(s, gw) for s in rows.scalars().all()]


@router.post("", response_model=VideoSourceOut, status_code=status.HTTP_201_CREATED)
async def register_source(body: VideoSourceIn, user: CurrentUser, db: DbSession, gw: Gateway) -> VideoSourceOut:
    """Validate first, then register. Invalid URLs are never stored."""
    source = gw.build(body.kind, body.uri)
    metadata = await gw.validate(source)
    record = VideoSourceRecord(
        owner_id=user.id,
        name=body.name.strip(),
        location=(body.location or "").strip() or None,
        kind=source.kind.value,
        uri=source.uri,
        status="ready",
        source_metadata=metadata,
        last_validated_at=datetime.now(UTC),
    )
    db.add(record)
    await db.commit()
    return serialize_source(record, gw)


@router.post("/upload", response_model=VideoSourceOut, status_code=status.HTTP_201_CREATED)
async def upload_source(
    user: CurrentUser,
    db: DbSession,
    gw: Gateway,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form(min_length=1, max_length=200)],
    location: Annotated[str | None, Form(max_length=200)] = None,
) -> VideoSourceOut:
    """Upload a recorded video file and register it as a source."""
    settings = get_settings()
    if not settings.enable_video_uploads:
        raise ValidationFailed("Video uploads are disabled on this server")
    relative, metadata = await store_upload(file, settings.upload_dir, user.id, settings.max_upload_bytes)
    record = VideoSourceRecord(
        owner_id=user.id,
        name=name.strip(),
        location=(location or "").strip() or None,
        kind="upload",
        uri=relative,
        status="ready",
        source_metadata=metadata,
        last_validated_at=datetime.now(UTC),
    )
    db.add(record)
    await db.commit()
    return serialize_source(record, gw)


@router.get("/{source_id}", response_model=VideoSourceOut)
async def get_source(source: OwnedSource, gw: Gateway) -> VideoSourceOut:
    return serialize_source(source, gw)


@router.patch("/{source_id}", response_model=VideoSourceOut)
async def update_source(body: VideoSourceUpdate, source: OwnedSource, db: DbSession, gw: Gateway) -> VideoSourceOut:
    if body.name is not None:
        source.name = body.name.strip()
    if "location" in body.model_fields_set:
        source.location = (body.location or "").strip() or None
    await db.commit()
    return serialize_source(source, gw)


@router.post("/{source_id}/revalidate", response_model=VideoSourceOut)
async def revalidate_source(source: OwnedSource, db: DbSession, gw: Gateway) -> VideoSourceOut:
    try:
        metadata = await gw.validate(gw.build(source.kind, source.uri, source_id=source.id))
        source.status, source.status_message = "ready", None
        source.source_metadata = {**source.source_metadata, **metadata}
    except ValidationFailed as exc:
        source.status, source.status_message = "error", exc.message
    source.last_validated_at = datetime.now(UTC)
    await db.commit()
    return serialize_source(source, gw)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source: OwnedSource, db: DbSession, gw: Gateway, analyzer: Analyzer) -> None:
    sessions = (await db.execute(select(VideoSession).where(VideoSession.video_source_id == source.id))).scalars().all()
    for session in sessions:
        if session.provider_ref and session.analyzer == analyzer.name:
            await analyzer.discard(PreparedVideo(analyzer=session.analyzer, ref=session.provider_ref))
    built = gw.build(source.kind, source.uri, source_id=source.id) if source.kind == "upload" else None
    await db.delete(source)
    await db.commit()
    if isinstance(built, UploadedVideoSource):
        built.delete_file()  # uploaded footage is not kept after its source is removed


@router.post("/{source_id}/open", response_model=VideoSessionOut)
async def open_source(source: OwnedSource, user: CurrentUser, db: DbSession, gw: Gateway, analyzer: Analyzer) -> VideoSession:
    """User opened the video: start preparing it for analysis in the background."""
    if source.status != "ready":
        raise ValidationFailed("This video source is not ready", code="source_not_ready")
    session = await VideoSessionService(db, gw, analyzer).open(source, user.id)
    if session.status == "preparing":
        schedule_prepare(session.id, gw, analyzer)
    return session


@router.get("/{source_id}/sessions/{session_id}", response_model=VideoSessionOut)
async def get_session(session_id: uuid.UUID, source: OwnedSource, db: DbSession) -> VideoSession:
    session = await db.get(VideoSession, session_id)
    if session is None or session.video_source_id != source.id:
        raise NotFoundError("Session not found")
    return session


@router.get("/{source_id}/events", response_model=list[VideoEventOut])
async def list_events(source: OwnedSource, db: DbSession, event_type: str | None = None, limit: int = 100) -> list[VideoEvent]:
    query = select(VideoEvent).where(VideoEvent.video_source_id == source.id)
    if event_type:
        query = query.where(VideoEvent.event_type == event_type)
    rows = await db.execute(query.order_by(VideoEvent.start_time.asc().nulls_last()).limit(min(limit, 500)))
    return list(rows.scalars().all())


@router.get("/{source_id}/stream")
async def stream_local(source: OwnedSource, gw: Gateway) -> FileResponse:
    """Serves uploaded and local videos to the player (supports Range requests)."""
    built = gw.build(source.kind, source.uri, source_id=source.id, metadata=source.source_metadata)
    if not isinstance(built, LocalFileVideoSource):
        raise NotFoundError("This source is not served by the backend")
    return FileResponse(built.file_path(), media_type=source.source_metadata.get("mime_type", "video/mp4"))
