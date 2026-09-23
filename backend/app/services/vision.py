"""Runs the local vision pipeline for a source and stores the results.

Runs are queued per source and executed one at a time (a single GPU), in a
worker thread so the API stays responsive. Phase 1 processes recorded media
(URL downloads and uploads). A live source would run the same pipeline
continuously over its stream instead of once.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.models import ObjectTrack, SceneSnapshot, VideoEvent, VideoSourceRecord, VisionRun
from app.video.gateway import VideoGateway

logger = logging.getLogger(__name__)

_gpu_lock = asyncio.Lock()
_progress: dict[uuid.UUID, float] = {}
_tasks: set[asyncio.Task[None]] = set()


def vision_available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return settings.vision_enabled and importlib.util.find_spec("ultralytics") is not None


def resolve_device(setting: str) -> str:
    if setting != "auto":
        return setting
    import torch

    return "cuda:0" if torch.cuda.is_available() else "cpu"


# One detector resident at a time: switching models costs a reload (~1 s) but keeps
# GPU memory per run attributable to that model, so comparisons are fair.
_resident: dict[str, object] = {}


def _free_gpu_memory() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:  # pragma: no cover
        pass


def _detector(name: str, weights: str | None, device: str, confidence: float, weights_dir: str):  # type: ignore[no-untyped-def]
    from pathlib import Path

    from app.vision.detectors import build_detector

    key = (name, weights, device, confidence, weights_dir)
    if _resident.get("key") != key:
        _resident.clear()
        _free_gpu_memory()
        detector = build_detector(name, weights, weights_dir=Path(weights_dir), device=device, confidence=confidence)
        detector.warmup()
        _resident.update(key=key, detector=detector)
    return _resident["detector"]


def run_progress(run: VisionRun) -> float:
    if run.status == "completed":
        return 1.0
    return _progress.get(run.id, 0.0)


DEFAULT_WEIGHTS = {"yolo": "yolo26s.pt", "rtdetr": "rtdetr-l.pt"}
DETECTORS = ("yolo", "rtdetr")
TRACKERS = ("bytetrack", "botsort")


def weights_for(detector: str, settings: Settings) -> str:
    """Configured weights apply to the configured detector; others use their default."""
    if detector == settings.vision_detector and settings.vision_weights:
        return settings.vision_weights
    return DEFAULT_WEIGHTS[detector]


def artifact_path(run_id: uuid.UUID) -> Path:
    return get_settings().vision_artifacts_dir / f"{run_id}.json"


class VisionService:
    """Runs are kept per (detector, tracker) combination so models can be compared
    on the same footage. The *primary* run (the configured default combination,
    else the newest completed one) is what the conversational agent reads."""

    def __init__(self, db: AsyncSession, gateway: VideoGateway) -> None:
        self.db = db
        self.gateway = gateway
        self.settings = get_settings()

    async def runs(self, source_id: uuid.UUID) -> list[VisionRun]:
        return list((await self.db.execute(select(VisionRun).where(VisionRun.video_source_id == source_id).order_by(VisionRun.created_at.desc()))).scalars().all())

    async def latest_run(self, source_id: uuid.UUID, detector: str | None = None, tracker: str | None = None) -> VisionRun | None:
        """Latest run of a combination (default: the configured one)."""
        detector = detector or self.settings.vision_detector
        tracker = tracker or self.settings.vision_tracker
        return (
            await self.db.execute(
                select(VisionRun)
                .where(VisionRun.video_source_id == source_id, VisionRun.detector == detector, VisionRun.tracker == tracker)
                .order_by(VisionRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def start(self, source: VideoSourceRecord, detector: str | None = None, tracker: str | None = None) -> VisionRun:
        s = self.settings
        detector = detector or s.vision_detector
        tracker = tracker or s.vision_tracker
        if detector not in DETECTORS or tracker not in TRACKERS:
            raise ValueError(f"Unknown combination {detector}+{tracker}")
        run = VisionRun(
            video_source_id=source.id,
            status="queued",
            detector=detector,
            weights=weights_for(detector, s),
            tracker=tracker,
            config={"scene": source.scene_config, "sample_fps": s.vision_sample_fps, "confidence": s.vision_confidence},
        )
        self.db.add(run)
        await self.db.commit()
        task = asyncio.create_task(_execute(run.id, self.gateway))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return run

    async def ensure(self, source: VideoSourceRecord) -> VisionRun | None:
        """Start a run unless one exists (queued, running or completed)."""
        if not vision_available(self.settings):
            return None
        latest = await self.latest_run(source.id)
        if latest is not None and latest.status in ("queued", "running", "completed"):
            return latest
        return await self.start(source)


async def _execute(run_id: uuid.UUID, gateway: VideoGateway) -> None:
    settings = get_settings()
    async with _gpu_lock, get_sessionmaker()() as db:
        run = await db.get(VisionRun, run_id)
        if run is None:
            return
        source = await db.get(VideoSourceRecord, run.video_source_id)
        if source is None:
            return
        run.status = "running"
        await db.commit()
        media = None
        try:
            from app.vision.events import SceneConfig
            from app.vision.pipeline import VisionPipeline
            from app.vision.trackers import build_tracker

            media = await gateway.acquire(gateway.build(source.kind, source.uri, source_id=source.id, metadata=source.source_metadata))
            if media.local_path is None:
                raise ValueError("Local analysis needs the video file. YouTube links are analysed by Gemini only.")
            device = resolve_device(settings.vision_device)
            detector = await asyncio.to_thread(
                _detector, run.detector, run.weights, device, settings.vision_confidence, str(settings.vision_weights_dir)
            )
            pipeline = VisionPipeline(
                detector,
                build_tracker(run.tracker, processing_fps=settings.vision_sample_fps),
                SceneConfig.from_dict(source.scene_config),
                sample_fps=settings.vision_sample_fps,
            )
            result = await asyncio.to_thread(pipeline.run, media.local_path, progress=lambda p: _progress.__setitem__(run_id, p))
            await _persist(db, run, source, result)
            logger.info("Vision run %s: %s tracks, %s events", run_id, len(result.tracks), len(result.events))
        except Exception as exc:
            logger.exception("Vision run %s failed", run_id)
            await db.rollback()
            run = await db.get(VisionRun, run_id)
            if run is not None:
                run.status = "failed"
                run.error = str(exc)[:500] if isinstance(exc, ValueError) else "Local video analysis failed"
                run.completed_at = datetime.now(UTC)
                await db.commit()
        finally:
            _progress.pop(run_id, None)
            if media is not None:
                await media.release()


async def _persist(db: AsyncSession, run: VisionRun, source: VideoSourceRecord, result) -> None:  # type: ignore[no-untyped-def]
    def wall(seconds: float | None) -> datetime | None:
        if source.recorded_start_at is None or seconds is None:
            return None
        start = source.recorded_start_at if source.recorded_start_at.tzinfo else source.recorded_start_at.replace(tzinfo=UTC)
        return start + timedelta(seconds=seconds)

    for t in result.tracks.values():
        db.add(
            ObjectTrack(
                vision_run_id=run.id,
                video_source_id=source.id,
                track_id=t.track_id,
                object_class=t.object_class,
                first_seen=round(t.first_seen, 2),
                last_seen=round(t.last_seen, 2),
                frames=t.frames,
                mean_confidence=round(t.mean_confidence, 3),
                max_confidence=round(t.max_confidence, 3),
                track_metadata={"class_votes": dict(t.classes), "first_bbox": [round(v, 1) for v in t.first_bbox], "last_bbox": [round(v, 1) for v in t.last_bbox]},
            )
        )
    for snap in result.snapshots:
        db.add(SceneSnapshot(vision_run_id=run.id, timestamp=snap.timestamp, counts=snap.counts, track_ids=snap.track_ids))
    detector_label = f"local:{run.detector}+{run.tracker}"
    for e in result.events:
        db.add(
            VideoEvent(
                video_source_id=source.id,
                vision_run_id=run.id,
                event_type=e.event_type,
                evidence_level=e.evidence_level.value,
                object_class=e.object_class,
                track_id=e.track_id,
                zone=e.zone,
                description=e.description,
                start_time=round(e.timestamp, 2),
                end_time=round(e.end_timestamp, 2) if e.end_timestamp is not None else None,
                occurred_at=wall(e.timestamp),
                confidence=e.confidence,
                detector=detector_label[:32],
                event_metadata=e.metadata,
            )
        )
    artifact = artifact_path(run.id)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"resolution": result.stats.get("resolution"), "sample_fps": result.stats.get("sample_fps"), "frames": result.frames}, separators=(",", ":")))

    run.status = "completed"
    run.stats = result.stats
    run.duration_seconds = result.stats.get("duration_seconds")
    run.completed_at = datetime.now(UTC)
    if run.duration_seconds and not source.source_metadata.get("duration_seconds"):
        source.source_metadata = {**source.source_metadata, "duration_seconds": run.duration_seconds}

    # Keep only the latest run per (source, detector, tracker): older runs of the
    # same combination are replaced; other combinations stay for comparison.
    same_combo = (
        VisionRun.video_source_id == source.id,
        VisionRun.detector == run.detector,
        VisionRun.tracker == run.tracker,
        VisionRun.id != run.id,
        VisionRun.status.in_(("completed", "failed")),
    )
    old_ids = list((await db.execute(select(VisionRun.id).where(*same_combo))).scalars().all())
    if old_ids:
        for model, column in ((VideoEvent, VideoEvent.vision_run_id), (ObjectTrack, ObjectTrack.vision_run_id), (SceneSnapshot, SceneSnapshot.vision_run_id)):
            await db.execute(delete(model).where(column.in_(old_ids)))
        await db.execute(delete(VisionRun).where(VisionRun.id.in_(old_ids)))
        for old_id in old_ids:
            artifact_path(old_id).unlink(missing_ok=True)
    await db.commit()
