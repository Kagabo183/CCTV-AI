from __future__ import annotations

import asyncio
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
from app.models import VideoEvent, VideoSession, VideoSourceRecord, VisionRun
from app.schemas.api import PlaybackOut, VideoEventOut, VideoSessionOut, VideoSourceIn, VideoSourceOut, VideoSourceUpdate
from app.video.sources.url_source import youtube_video_id
from app.video.url_safety import is_youtube
from app.services.imports import cancel_import, import_progress, schedule_import, schedule_normalize
from app.services.video_sessions import VideoSessionService, schedule_prepare
from app.services.vision import VisionService, request_cancel
from app.video.gateway import VideoGateway
from app.video.sources.local_source import LocalFileVideoSource
from app.video import ffmpeg, ingest
from app.video.sources.upload_source import StoredVideoSource, store_upload

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
        playback=playback if source.status == "ready" else None,
        import_progress=import_progress(source.id) if source.status == "importing" else None,
        created_at=source.created_at,
    )


@router.get("", response_model=list[VideoSourceOut])
async def list_sources(user: CurrentUser, db: DbSession, gw: Gateway) -> list[VideoSourceOut]:
    rows = await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.owner_id == user.id).order_by(VideoSourceRecord.created_at.desc()))
    return [serialize_source(s, gw) for s in rows.scalars().all()]


def _default_name(plan: ingest.ImportPlan) -> str:
    if plan.title:
        return plan.title[:200]
    from urllib.parse import urlsplit

    parts = urlsplit(plan.display_url)
    tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return (tail or parts.hostname or "Camera")[:200]


def _streamable(plan: ingest.ImportPlan) -> bool:
    """Recorded YouTube videos are watched in place instead of downloaded."""
    return plan.kind == "site" and not plan.is_live and is_youtube(plan.url) and youtube_video_id(plan.url) is not None


def _stream_metadata(plan: ingest.ImportPlan) -> dict:
    meta = {"delivery": "youtube", "youtube_id": youtube_video_id(plan.url), "original_url": plan.display_url, "extractor": plan.extractor, "analysis": "stream"}
    if plan.title:
        meta["title"] = plan.title[:300]
    if plan.duration:
        meta["duration_seconds"] = plan.duration
    return meta


@router.post("", response_model=VideoSourceOut, status_code=status.HTTP_201_CREATED)
async def register_source(body: VideoSourceIn, user: CurrentUser, db: DbSession, gw: Gateway) -> VideoSourceOut:
    """Register a link. It is checked now (unreachable or unsafe links are never
    stored), then imported in the background as a playable MP4."""
    if body.kind == "local":
        source = gw.build("local", body.uri)
        record = VideoSourceRecord(
            owner_id=user.id, name=(body.name or body.uri).strip()[:200], location=(body.location or "").strip() or None,
            kind="local", uri=source.uri, status="ready", source_metadata=await gw.validate(source), last_validated_at=datetime.now(UTC),
        )
        db.add(record)
        await db.commit()
        await VisionService(db, gw).ensure(record)
        return serialize_source(record, gw)
    if body.kind in ("onvif", "nvr"):
        gw.build(body.kind, body.uri)  # raises "not supported yet"

    plan = await ingest.plan(body.uri, gw.import_settings(body.clip_seconds))
    if _streamable(plan):
        # Plays in the YouTube player immediately; local vision reads the online stream (no download).
        record = VideoSourceRecord(
            owner_id=user.id,
            name=(body.name or "").strip() or _default_name(plan),
            location=(body.location or "").strip() or None,
            kind="url",
            uri=plan.url,
            status="ready",
            source_metadata=_stream_metadata(plan),
            last_validated_at=datetime.now(UTC),
        )
        db.add(record)
        await db.commit()
        await VisionService(db, gw).ensure(record)
        return serialize_source(record, gw)
    record = VideoSourceRecord(
        owner_id=user.id,
        name=(body.name or "").strip() or _default_name(plan),
        location=(body.location or "").strip() or None,
        kind="rtsp" if plan.details.get("protocol") in ("rtsp", "rtsps") else "url",
        uri=plan.display_url,  # never contains credentials
        status="importing",
        source_metadata={"delivery": "importing", "import_kind": plan.kind, "original_url": plan.display_url, "title": plan.title, "is_live": plan.is_live, "extractor": plan.extractor},
    )
    db.add(record)
    await db.commit()
    schedule_import(record.id, plan, gw, body.clip_seconds)
    return serialize_source(record, gw)


@router.post("/{source_id}/import", response_model=VideoSourceOut)
async def reimport_source(source: OwnedSource, db: DbSession, gw: Gateway, clip_seconds: int | None = None) -> VideoSourceOut:
    """(Re)import a link source, e.g. a YouTube link added before imports existed,
    or a live stream to record a fresh clip. Camera passwords are never stored,
    so password-protected streams must be added again instead."""
    if source.kind == "upload" or source.status == "importing":
        raise ValidationFailed("This source cannot be imported again", code="not_importable")
    plan = await ingest.plan(source.uri, gw.import_settings(clip_seconds))
    if _streamable(plan):
        source.status, source.status_message = "ready", None
        source.source_metadata = {**_stream_metadata(plan), **{k: v for k, v in source.source_metadata.items() if k in ("duration_seconds", "width", "height", "fps")}}
        await db.commit()
        await VisionService(db, gw).ensure(source)
        return serialize_source(source, gw)
    source.status, source.status_message = "importing", None
    source.source_metadata = {**source.source_metadata, "import_kind": plan.kind, "title": plan.title or source.source_metadata.get("title")}
    await db.commit()
    schedule_import(source.id, plan, gw, clip_seconds)
    return serialize_source(source, gw)


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
    try:
        info = await asyncio.to_thread(ffmpeg.probe, settings.upload_dir.resolve() / relative)
        metadata.update(info.as_metadata())
        playable = info.browser_ready
    except ffmpeg.FfmpegError:
        playable = True  # let OpenCV/Gemini try; the browser may still play it
    record = VideoSourceRecord(
        owner_id=user.id,
        name=name.strip(),
        location=(location or "").strip() or None,
        kind="upload",
        uri=relative,
        status="ready" if playable else "importing",
        source_metadata=metadata,
        last_validated_at=datetime.now(UTC),
    )
    db.add(record)
    await db.commit()
    if playable:
        await VisionService(db, gw).ensure(record)  # local detection/tracking/events in the background
    else:
        schedule_normalize(record.id, gw)  # e.g. HEVC/AVI export: convert to H.264 MP4 first
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
    if source.status == "importing":
        cancel_import(source.id)  # stop the download; its partial files are removed by the import itself
    active = await db.execute(select(VisionRun.id).where(VisionRun.video_source_id == source.id, VisionRun.status.in_(("queued", "running"))))
    for run_id in active.scalars().all():
        request_cancel(run_id)  # otherwise a deleted video's analysis keeps the GPU busy
    sessions = (await db.execute(select(VideoSession).where(VideoSession.video_source_id == source.id))).scalars().all()
    for session in sessions:
        if session.provider_ref and session.analyzer == analyzer.name:
            await analyzer.discard(PreparedVideo(analyzer=session.analyzer, ref=session.provider_ref))
    try:
        built = gw.build(source.kind, source.uri, source_id=source.id, metadata=source.source_metadata)
    except AppError:
        built = None
    await db.delete(source)
    await db.commit()
    if isinstance(built, StoredVideoSource):
        built.delete_file()  # stored footage (uploads, imported links) is not kept after its source is removed


@router.post("/{source_id}/open", response_model=VideoSessionOut)
async def open_source(source: OwnedSource, user: CurrentUser, db: DbSession, gw: Gateway, analyzer: Analyzer) -> VideoSession:
    """User opened the video: start preparing it for analysis in the background."""
    if source.status != "ready":
        raise ValidationFailed("This video source is not ready", code="source_not_ready")
    session = await VideoSessionService(db, gw, analyzer).open(source, user.id)
    if session.status == "preparing":
        schedule_prepare(session.id, gw, analyzer)
    await VisionService(db, gw).ensure(source)
    return session


@router.get("/{source_id}/sessions/{session_id}", response_model=VideoSessionOut)
async def get_session(session_id: uuid.UUID, source: OwnedSource, db: DbSession) -> VideoSession:
    session = await db.get(VideoSession, session_id)
    if session is None or session.video_source_id != source.id:
        raise NotFoundError("Session not found")
    return session


@router.get("/{source_id}/events", response_model=list[VideoEventOut])
async def list_events(
    source: OwnedSource,
    db: DbSession,
    event_type: str | None = None,
    evidence_level: str | None = None,
    vision_run_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[VideoEvent]:
    query = select(VideoEvent).where(VideoEvent.video_source_id == source.id)
    if vision_run_id:
        query = query.where(VideoEvent.vision_run_id == vision_run_id)
    if event_type:
        query = query.where(VideoEvent.event_type == event_type)
    if evidence_level:
        query = query.where(VideoEvent.evidence_level == evidence_level)
    rows = await db.execute(query.order_by(VideoEvent.start_time.asc().nulls_last()).limit(min(limit, 500)))
    return list(rows.scalars().all())


@router.get("/{source_id}/stream")
async def stream_local(source: OwnedSource, gw: Gateway) -> FileResponse:
    """Serves uploaded and local videos to the player (supports Range requests)."""
    built = gw.build(source.kind, source.uri, source_id=source.id, metadata=source.source_metadata)
    if not isinstance(built, LocalFileVideoSource):
        raise NotFoundError("This source is not served by the backend")
    return FileResponse(built.file_path(), media_type=source.source_metadata.get("mime_type", "video/mp4"))
