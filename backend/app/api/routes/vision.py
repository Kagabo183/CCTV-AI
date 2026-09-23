from __future__ import annotations

import uuid
from datetime import datetime
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

MODEL_LABELS = {"yolo": "YOLO26s", "rtdetr": "RT-DETR-L", "bytetrack": "ByteTrack", "botsort": "BoT-SORT"}


class VisionRunOut(BaseModel):
    id: uuid.UUID | None = None
    available: bool = True
    unsupported_reason: str | None = None  # e.g. YouTube links: local analysis impossible
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


class RunIn(BaseModel):
    detector: Literal["yolo", "rtdetr"] | None = None
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
    return [await _describe(db, run) for run in await VisionService(db, gw).runs(source.id)]


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
