from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DbSession, Gateway, owned_source
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationFailed
from app.models import ObjectTrack, VideoEvent, VideoSourceRecord, VisionRun
from app.services.vision import VisionService, artifact_path, local_analysis_blocker, run_progress, vision_available

router = APIRouter(prefix="/video-sources/{source_id}", tags=["local vision"])
OwnedSource = Annotated[VideoSourceRecord, Depends(owned_source)]

MODEL_LABELS = {"yolo": "YOLO26s", "yolo_o365": "YOLO26s-O365", "rtdetr": "RT-DETR-L", "wildlife": "MegaDetector V6", "bytetrack": "ByteTrack", "botsort": "BoT-SORT"}


class VisionRunOut(BaseModel):
    id: uuid.UUID | None = None
    available: bool = True
    unsupported_reason: str | None = None  # e.g. YouTube links: local analysis impossible
    queue_position: int | None = None  # 1 = next to run (runs execute one at a time on the GPU)
    waiting_for: str | None = None  # the run currently using the GPU
    status: str  # disabled | not_started | queued | running | completed | failed
    progress: float = 0.0
    detector: str | None = None
    weights: str | None = None
    tracker: str | None = None
    label: str | None = None
    is_primary: bool = False  # the run the conversational agent answers from
    error: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    tracks_by_class: dict[str, int] = Field(default_factory=dict)
    events_by_type: dict[str, int] = Field(default_factory=dict)
    performance: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    max_simultaneous: dict[str, dict[str, float]] = Field(default_factory=dict)  # class -> {count, at}
    settings: dict[str, Any] = Field(default_factory=dict)  # image size, tiling, confidence of the detector


class RunIn(BaseModel):
    detector: Literal["yolo", "yolo_o365", "rtdetr", "wildlife"] | None = None
    tracker: Literal["bytetrack", "botsort"] | None = None


class SceneIn(BaseModel):
    """Zones/tripwires in normalised [0,1] image coordinates, plus rule thresholds."""

    zones: list[dict[str, Any]] = Field(default_factory=list)
    lines: list[dict[str, Any]] = Field(default_factory=list)
    rules: dict[str, Any] = Field(default_factory=dict)
    recorded_start_at: datetime | None = None
    rerun: bool = True


def _is_primary(run: VisionRun) -> bool:
    s = get_settings()
    return run.detector == s.vision_detector and run.tracker == s.vision_tracker


async def _describe(db: DbSession, run: VisionRun | None) -> VisionRunOut:
    if not vision_available():
        return VisionRunOut(available=False, status="disabled")
    if run is None:
        return VisionRunOut(status="not_started")
    out = VisionRunOut(
        id=run.id,
        status=run.status,
        progress=round(run_progress(run), 3),
        detector=run.detector,
        weights=run.weights,
        tracker=run.tracker,
        label=f"{MODEL_LABELS.get(run.detector, run.detector)} + {MODEL_LABELS.get(run.tracker, run.tracker)}",
        is_primary=_is_primary(run),
        error=run.error,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )
    if run.status == "completed":
        tracks = (await db.execute(select(ObjectTrack.object_class, func.count()).where(ObjectTrack.vision_run_id == run.id).group_by(ObjectTrack.object_class))).all()
        events = (await db.execute(select(VideoEvent.event_type, func.count()).where(VideoEvent.vision_run_id == run.id).group_by(VideoEvent.event_type))).all()
        out.tracks_by_class = {k: v for k, v in tracks}
        out.events_by_type = {k: v for k, v in events}
        st = run.stats
        out.performance = {k: st.get(k) for k in ("processing_fps", "realtime_factor", "detect_ms_p50", "detect_ms_p95", "track_ms_p50", "gpu_peak_mb", "cpu_percent_avg", "sample_fps", "duration_seconds", "resolution", "frames_processed")}
        out.quality = {"tracks": st.get("tracks", {}), "detections": st.get("detections", {})}
        out.max_simultaneous = st.get("max_simultaneous", {})
        model = st.get("model", {})
        out.settings = {k: model.get(k) for k in ("weights", "architecture", "dataset", "num_classes", "confidence", "image_size", "tiling")} | {"confirm_confidence": st.get("confirm_confidence")}
    return out


async def _owned_run(source: VideoSourceRecord, run_id: uuid.UUID, db: DbSession) -> VisionRun:
    run = await db.get(VisionRun, run_id)
    if run is None or run.video_source_id != source.id:
        raise NotFoundError("Vision run not found")
    return run


@router.get("/vision", response_model=VisionRunOut)
async def vision_status(source: OwnedSource, db: DbSession, gw: Gateway) -> VisionRunOut:
    """The primary run (configured detector + tracker)."""
    return await _describe(db, await VisionService(db, gw).latest_run(source.id))


@router.get("/vision/runs", response_model=list[VisionRunOut])
async def vision_runs(source: OwnedSource, db: DbSession, gw: Gateway) -> list[VisionRunOut]:
    """Every model combination run on this source, for side-by-side comparison."""
    if not vision_available():
        return [VisionRunOut(available=False, status="disabled")]
    if (reason := local_analysis_blocker(source)) is not None:
        return [VisionRunOut(status="unsupported", unsupported_reason=reason)]
    service = VisionService(db, gw)
    out = []
    for run in await service.runs(source.id):
        described = await _describe(db, run)
        described.queue_position, described.waiting_for = await service.queue_info(run)
        out.append(described)
    return out


@router.post("/vision/runs/{run_id}/cancel", response_model=VisionRunOut)
async def cancel_run(run_id: uuid.UUID, source: OwnedSource, db: DbSession) -> VisionRunOut:
    """Cancel a queued or running analysis (e.g. a long video blocking the GPU queue)."""
    from app.services.vision import request_cancel

    run = await _owned_run(source, run_id, db)
    if run.status not in ("queued", "running"):
        raise ValidationFailed("Only queued or running analyses can be cancelled")
    request_cancel(run.id)
    if run.status == "queued":
        run.status, run.error = "cancelled", "Cancelled by user"
        await db.commit()
    return await _describe(db, run)


@router.post("/vision/run", response_model=VisionRunOut)
async def run_vision(source: OwnedSource, db: DbSession, gw: Gateway, body: RunIn | None = None) -> VisionRunOut:
    """(Re)run local analysis with a detector + tracker (default: the configured ones)."""
    if not vision_available():
        raise ValidationFailed("Local vision is disabled on this server")
    body = body or RunIn()
    service = VisionService(db, gw)
    latest = await service.latest_run(source.id, body.detector, body.tracker)
    if latest is not None and latest.status in ("queued", "running"):
        return await _describe(db, latest)
    return await _describe(db, await service.start(source, body.detector, body.tracker))


@router.get("/vision/runs/{run_id}/boxes")
async def run_boxes(run_id: uuid.UUID, source: OwnedSource, db: DbSession) -> FileResponse:
    """Per-frame tracked boxes, drawn by the UI over the original video."""
    run = await _owned_run(source, run_id, db)
    path = artifact_path(run.id)
    if run.status != "completed" or not path.exists():
        raise NotFoundError("No box data for this run")
    return FileResponse(path, media_type="application/json")


class TrackOut(BaseModel):
    track_id: int
    object_class: str  # "unknown" when the detector was not confident or kept changing its mind
    candidate_class: str | None
    uncertain: bool
    mean_confidence: float
    max_confidence: float
    first_seen: float
    last_seen: float
    frames: int
    class_votes: dict[str, int]
    last_bbox: list[float]
    wildlife: dict[str, Any] | None = None  # species verdict (wildlife runs): species/candidate/certain/score/display


class DescribeOut(BaseModel):
    track_id: int
    description: str
    observations: list[str]
    confidence: float | None
    insufficient_evidence: bool
    provider: str
    model: str | None
    window: list[float]
    evidence_level: str = "model_interpretation"


def _track_out(t: ObjectTrack) -> TrackOut:
    m = t.track_metadata or {}
    return TrackOut(
        track_id=t.track_id,
        object_class=t.object_class,
        candidate_class=m.get("candidate_class", t.object_class),
        uncertain=bool(m.get("uncertain", False)),
        mean_confidence=t.mean_confidence,
        max_confidence=t.max_confidence,
        first_seen=t.first_seen,
        last_seen=t.last_seen,
        frames=t.frames,
        class_votes=m.get("class_votes", {}),
        last_bbox=m.get("last_bbox", []),
        wildlife=m.get("wildlife"),
    )


@router.get("/vision/runs/{run_id}/tracks", response_model=list[TrackOut])
async def run_tracks(run_id: uuid.UUID, source: OwnedSource, db: DbSession) -> list[TrackOut]:
    """Every tracked object of a run, with class, confidence and candidate class for uncertain ones."""
    run = await _owned_run(source, run_id, db)
    rows = (await db.execute(select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id).order_by(ObjectTrack.first_seen))).scalars().all()
    return [_track_out(t) for t in rows]


@router.post("/vision/runs/{run_id}/tracks/{track_id}/describe", response_model=DescribeOut)
async def describe_track(run_id: uuid.UUID, track_id: int, source: OwnedSource, db: DbSession, gw: Gateway) -> DescribeOut:
    """Ask a vision-language model what this tracked object actually is (open vocabulary).

    The answer is stored as a model_interpretation event on the track; it never
    overwrites the detector's own label."""
    from app.analyzers.base import SourceContext
    from app.analyzers.factory import get_video_analyzer
    from app.services.video_sessions import VideoSessionService, _prepared_from
    from app.understanding.base import UnderstandingRequest, VideoRef
    from app.understanding.factory import get_understanding_provider

    run = await _owned_run(source, run_id, db)
    track = (await db.execute(select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id, ObjectTrack.track_id == track_id))).scalar_one_or_none()
    if track is None:
        raise NotFoundError("Track not found")
    start, end = max(0.0, track.first_seen - 0.5), track.last_seen + 0.5
    candidate = (track.track_metadata or {}).get("candidate_class", track.object_class)
    box = (track.track_metadata or {}).get("last_bbox")
    question = (
        f"An object detector tracked something (track #{track_id}) between {start:.1f}s and {end:.1f}s and guessed it is a '{candidate}' "
        f"(confidence {track.mean_confidence:.2f}){f', last seen at box {box} (pixels)' if box else ''}. "
        "What is it really? Name the object or animal precisely, say whether the detector's guess is right, and describe what it is doing."
    )
    settings = get_settings()
    stored = source.source_metadata.get("stored_path") or (source.uri if source.kind == "upload" else None)
    prepared = None
    try:  # reuse the analyzer's prepared upload (Gemini) if one exists
        analyzer = get_video_analyzer()
        session = await VideoSessionService(db, gw, analyzer).open(source, source.owner_id)
        prepared = _prepared_from(session)
    except Exception:  # noqa: BLE001 - frame-based providers do not need it
        prepared = None
    local: Path | str | None = (settings.upload_dir / stored).resolve() if stored else None
    if local is None:
        from app.video import streaming

        if streaming.is_streamed(source.source_metadata):  # online video: the VLM reads frames from the stream
            local = (await asyncio.to_thread(streaming.resolve, source.uri, min(720, settings.video_import_max_height))).url
    video = VideoRef(prepared=prepared, local_path=local)
    request = UnderstandingRequest(question=question, source=SourceContext(source_id=source.id, name=source.name, location=source.location, kind=source.kind), start_seconds=start, end_seconds=end)
    result = await get_understanding_provider().understand(video, request)

    db.add(VideoEvent(
        video_source_id=source.id, vision_run_id=run.id, event_type="observation", evidence_level="model_interpretation",
        object_class=None, track_id=track_id, description=result.description[:2000], start_time=round(start, 2), end_time=round(end, 2),
        confidence=result.confidence, detector=f"vlm:{result.provider}"[:32],
        event_metadata={"question": question, "observations": result.observations[:20], "detector_label": track.object_class, "detector_candidate": candidate, "model": result.model},
    ))
    await db.commit()
    return DescribeOut(
        track_id=track_id, description=result.description, observations=result.observations[:20], confidence=result.confidence,
        insufficient_evidence=result.insufficient_evidence, provider=result.provider, model=result.model, window=[round(start, 2), round(end, 2)],
    )


@router.get("/scene")
async def get_scene(source: OwnedSource) -> dict[str, Any]:
    return {**source.scene_config, "recorded_start_at": source.recorded_start_at}


@router.put("/scene")
async def put_scene(body: SceneIn, source: OwnedSource, db: DbSession, gw: Gateway) -> dict[str, Any]:
    from app.vision.events import SceneConfig

    try:
        scene = SceneConfig.from_dict({"zones": body.zones, "lines": body.lines, "rules": body.rules})
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationFailed(f"Invalid scene configuration: {exc}") from exc
    for zone in scene.zones:
        if len(zone.polygon) < 3 or not all(0 <= c <= 1 for p in zone.polygon for c in p):
            raise ValidationFailed(f"Zone '{zone.name}' needs at least 3 points with coordinates between 0 and 1")
    source.scene_config = scene.to_dict()
    source.recorded_start_at = body.recorded_start_at
    await db.commit()
    if body.rerun and vision_available() and local_analysis_blocker(source) is None:
        await VisionService(db, gw).start(source)  # events depend on zones: recompute
    return {**source.scene_config, "recorded_start_at": source.recorded_start_at}
