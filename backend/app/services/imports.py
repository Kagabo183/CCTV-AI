"""Background import of links (and re-encoding of uploads) into playable MP4s.

A source is registered immediately with status "importing". This service
fetches/records/converts the video, stores it under UPLOAD_DIR/<owner>/<source>.mp4,
marks the source "ready" (or "error" with a reason), then starts local vision.

Plans can contain camera credentials: they live only in memory for the
duration of the import and are never written to the database or logs.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models import VideoSourceRecord
from app.video import ffmpeg, ingest
from app.video.gateway import VideoGateway

logger = logging.getLogger(__name__)

_progress: dict[uuid.UUID, float] = {}
_tasks: set[asyncio.Task[None]] = set()
_by_source: dict[uuid.UUID, asyncio.Task[None]] = {}
_slots = asyncio.Semaphore(2)  # concurrent downloads/encodes


def import_progress(source_id: uuid.UUID) -> float | None:
    return _progress.get(source_id)


def stored_path_for(source: VideoSourceRecord) -> str:
    return f"{source.owner_id}/{source.id}.mp4"


def _spawn(coro, source_id: uuid.UUID | None = None) -> None:  # type: ignore[no-untyped-def]
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    if source_id is not None:
        _by_source[source_id] = task
        task.add_done_callback(lambda _t: _by_source.pop(source_id, None))


def schedule_import(source_id: uuid.UUID, plan: ingest.ImportPlan, gateway: VideoGateway, clip_seconds: float | None = None) -> None:
    _progress[source_id] = 0.0
    _spawn(_import(source_id, plan, gateway, clip_seconds), source_id)


def cancel_import(source_id: uuid.UUID) -> None:
    """Stop an import (e.g. its source was deleted): kill its download helpers, cancel the task."""
    import psutil

    marker = f"import_{source_id.hex}_"
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if any(marker in part for part in proc.info["cmdline"] or []):
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    task = _by_source.pop(source_id, None)
    if task is not None:
        task.cancel()
    _progress.pop(source_id, None)


def schedule_normalize(source_id: uuid.UUID, gateway: VideoGateway) -> None:
    """Re-encode an uploaded file the browser cannot play (e.g. HEVC CCTV export)."""
    _progress[source_id] = 0.0
    _spawn(_normalize_upload(source_id, gateway))


async def _finish(source_id: uuid.UUID, gateway: VideoGateway, *, metadata: dict | None, error: str | None) -> None:
    from app.services.vision import VisionService

    async with get_sessionmaker()() as db:
        source = await db.get(VideoSourceRecord, source_id)
        if source is None:  # deleted while importing
            return
        if error:
            source.status, source.status_message = "error", error[:500]
        else:
            source.status, source.status_message = "ready", None
            source.source_metadata = {**source.source_metadata, **(metadata or {})}
            source.last_validated_at = datetime.now(UTC)
        await db.commit()
        if not error:
            await VisionService(db, gateway).ensure(source)


async def _import(source_id: uuid.UUID, plan: ingest.ImportPlan, gateway: VideoGateway, clip_seconds: float | None) -> None:
    settings = gateway.import_settings(clip_seconds)
    async with _slots:
        async with get_sessionmaker()() as db:
            source = await db.get(VideoSourceRecord, source_id)
            if source is None:
                return
            relative = stored_path_for(source)
        dest = get_settings().upload_dir.resolve() / relative
        try:
            meta = await ingest.run(plan, dest, settings, lambda p: _progress.__setitem__(source_id, round(p, 3)), tag=source_id.hex)
            meta["stored_path"] = relative
            await _finish(source_id, gateway, metadata=meta, error=None)
            logger.info("Imported source %s (%s, %s s)", source_id, plan.kind, meta.get("duration_seconds"))
        except AppError as exc:
            dest.unlink(missing_ok=True)
            await _finish(source_id, gateway, metadata=None, error=exc.message)
        except Exception:
            logger.exception("Import of source %s failed", source_id)
            dest.unlink(missing_ok=True)
            await _finish(source_id, gateway, metadata=None, error="Import failed unexpectedly")
        finally:
            _progress.pop(source_id, None)


async def _normalize_upload(source_id: uuid.UUID, gateway: VideoGateway) -> None:
    settings = get_settings()
    async with _slots:
        async with get_sessionmaker()() as db:
            source = await db.get(VideoSourceRecord, source_id)
            if source is None:
                return
            original = settings.upload_dir.resolve() / source.uri
        converted = original.with_name(original.stem + ".web.mp4")
        try:
            _progress[source_id] = 0.2
            info = await asyncio.to_thread(ffmpeg.normalize, original, converted, max_height=settings.video_import_max_height)
            converted.replace(original.with_suffix(".mp4"))
            if original.suffix.lower() != ".mp4":
                original.unlink(missing_ok=True)
            async with get_sessionmaker()() as db:
                source = await db.get(VideoSourceRecord, source_id)
                if source is not None:
                    source.uri = str(Path(source.uri).with_suffix(".mp4")).replace("\\", "/")
                    await db.commit()
            await _finish(source_id, gateway, metadata={**info.as_metadata(), "mime_type": "video/mp4", "converted": True}, error=None)
        except ffmpeg.FfmpegError as exc:
            converted.unlink(missing_ok=True)
            await _finish(source_id, gateway, metadata=None, error=f"Could not convert this video: {exc}")
        finally:
            _progress.pop(source_id, None)
