"""Tools the conversational agent can call. The user never talks to YOLO or
ByteTrack directly: the agent decides which tool answers the question.

Local tools read the stored output of the vision engine (no GPU, no API cost):
    list_cameras, get_camera_status, get_current_objects, get_recent_events,
    count_objects, get_event_details, retrieve_video_clip
Escalation tool (costs a Gemini call on the video):
    analyze_video_clip

Every result states its evidence level: detections/tracks/rules are facts about
boxes, identities and geometry. Only analyze_video_clip returns a model's
*interpretation* of what is happening.

Time: for recorded videos, times are seconds from the start of the video and
"now" means the end of the recording. Clock times ("14:00") only work when the
source has recorded_start_at.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ObjectTrack, SceneSnapshot, VideoEvent, VideoSourceRecord, VisionRun

KIGALI = ZoneInfo("Africa/Kigali")
_MAX_EVENTS = 60


class ToolError(Exception):
    """Returned to the model as {"error": ...} so it can recover or explain."""


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


class VisionMemory:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID, default_source_id: uuid.UUID | None) -> None:
        self.db = db
        self.user_id = user_id
        self.default_source_id = default_source_id

    # -- helpers ---------------------------------------------------------------

    async def _source(self, camera_id: str | None) -> VideoSourceRecord:
        if not camera_id:
            if self.default_source_id is None:
                raise ToolError("No camera selected. Call list_cameras and pass camera_id.")
            source = await self.db.get(VideoSourceRecord, self.default_source_id)
        else:
            try:
                source = await self.db.get(VideoSourceRecord, uuid.UUID(str(camera_id)))
            except ValueError:
                needle = str(camera_id).lower()
                rows = (await self.db.execute(select(VideoSourceRecord).where(VideoSourceRecord.owner_id == self.user_id))).scalars().all()
                source = next((s for s in rows if needle in (s.location or "").lower() or needle in s.name.lower()), None)
        if source is None or source.owner_id != self.user_id:
            raise ToolError(f"Unknown camera: {camera_id}. Call list_cameras.")
        return source

    async def _run(self, source: VideoSourceRecord) -> VisionRun:
        """The primary run: the configured detector+tracker if completed, else the newest completed one."""
        settings = get_settings()
        runs = list((await self.db.execute(select(VisionRun).where(VisionRun.video_source_id == source.id).order_by(VisionRun.created_at.desc()))).scalars().all())
        done = [r for r in runs if r.status == "completed"]
        preferred = [r for r in done if r.detector == settings.vision_detector and r.tracker == settings.vision_tracker]
        completed = (preferred or done or [None])[0]
        run = runs[0] if runs else None
        if completed is None:
            state = run.status if run else "not_started"
            detail = f" ({run.error})" if run is not None and run.error else ""
            raise ToolError(f"Local analysis for this camera is not available: {state}{detail}. Use analyze_video_clip instead.")
        return completed

    def _range(self, source: VideoSourceRecord, run: VisionRun, args: dict[str, Any]) -> tuple[float, float]:
        """Resolve start/end in media seconds from seconds, 'last_seconds' or clock times."""
        duration = run.duration_seconds or 0.0
        start, end = 0.0, duration
        if args.get("last_seconds") is not None:
            start = max(0.0, duration - float(args["last_seconds"]))
        if args.get("start_seconds") is not None:
            start = float(args["start_seconds"])
        if args.get("end_seconds") is not None:
            end = float(args["end_seconds"])
        for key in ("start_clock", "end_clock"):
            if args.get(key):
                seconds = self._clock_to_seconds(source, str(args[key]))
                if key == "start_clock":
                    start = seconds
                else:
                    end = seconds
        if start > end:
            start, end = end, start
        return max(0.0, start), end

    @staticmethod
    def _clock_to_seconds(source: VideoSourceRecord, clock: str) -> float:
        if source.recorded_start_at is None:
            raise ToolError("This video has no known recording start time, so clock times like '14:00' cannot be mapped to it. Ask about positions in the video (e.g. 'minute 2') instead.")
        start = source.recorded_start_at if source.recorded_start_at.tzinfo else source.recorded_start_at.replace(tzinfo=UTC)
        local_start = start.astimezone(KIGALI)
        hour, minute = (int(p) for p in clock.split(":")[:2])
        target = local_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target < local_start - timedelta(hours=12):
            target += timedelta(days=1)
        return (target - local_start).total_seconds()

    # -- tools -----------------------------------------------------------------

    async def list_cameras(self) -> dict[str, Any]:
        rows = (await self.db.execute(select(VideoSourceRecord).where(VideoSourceRecord.owner_id == self.user_id))).scalars().all()
        return {"cameras": [{"camera_id": str(s.id), "name": s.name, "location": s.location, "selected": s.id == self.default_source_id} for s in rows]}

    async def get_camera_status(self, camera_id: str | None = None) -> dict[str, Any]:
        source = await self._source(camera_id)
        run = (
            await self.db.execute(select(VisionRun).where(VisionRun.video_source_id == source.id).order_by(VisionRun.created_at.desc()).limit(1))
        ).scalar_one_or_none()
        return {
            "camera_id": str(source.id),
            "name": source.name,
            "location": source.location,
            "source_type": source.kind,
            "is_live": False,
            "recorded_video_duration_seconds": source.source_metadata.get("duration_seconds") or (run.duration_seconds if run else None),
            "recorded_start_at_kigali": source.recorded_start_at.astimezone(KIGALI).isoformat() if source.recorded_start_at else None,
            "local_analysis": {
                "status": run.status if run else "not_started",
                "detector": f"{run.detector} ({run.weights})" if run else None,
                "tracker": run.tracker if run else None,
                "error": run.error if run else None,
                "zones_configured": [z["name"] for z in source.scene_config.get("zones", [])] + [ln["name"] for ln in source.scene_config.get("lines", [])],
            },
        }

    async def get_current_objects(self, camera_id: str | None = None, at_seconds: float | None = None) -> dict[str, Any]:
        source = await self._source(camera_id)
        run = await self._run(source)
        at = (run.duration_seconds or 0.0) if at_seconds is None else float(at_seconds)
        snap = (
            await self.db.execute(
                select(SceneSnapshot).where(SceneSnapshot.vision_run_id == run.id, SceneSnapshot.timestamp <= at + 0.5).order_by(SceneSnapshot.timestamp.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if snap is None:
            return {"evidence_level": "tracking", "at_seconds": _r(at), "objects": [], "counts": {}}
        tracks = (
            await self.db.execute(select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id, ObjectTrack.track_id.in_(snap.track_ids or [-1])))
        ).scalars().all()
        return {
            "evidence_level": "tracking",
            "at_seconds": snap.timestamp,
            "note": "Recorded video: 'now' is the end of the recording." if at_seconds is None else None,
            "counts": snap.counts,
            "objects": [
                {"track_id": t.track_id, "class": t.object_class, "visible_since_seconds": _r(t.first_seen), "seconds_in_view_so_far": _r(snap.timestamp - t.first_seen), "confidence": t.mean_confidence}
                for t in sorted(tracks, key=lambda t: t.track_id)
            ],
        }

    async def get_recent_events(
        self,
        camera_id: str | None = None,
        event_types: list[str] | None = None,
        object_class: str | None = None,
        **time_args: Any,
    ) -> dict[str, Any]:
        source = await self._source(camera_id)
        run = await self._run(source)
        start, end = self._range(source, run, time_args)
        query = select(VideoEvent).where(
            VideoEvent.vision_run_id == run.id,
            VideoEvent.start_time <= end,
            or_(and_(VideoEvent.end_time.is_(None), VideoEvent.start_time >= start), VideoEvent.end_time >= start),
        )
        if event_types:
            query = query.where(VideoEvent.event_type.in_(event_types))
        if object_class:
            query = query.where(VideoEvent.object_class == object_class)
        rows = (await self.db.execute(query.order_by(VideoEvent.start_time).limit(_MAX_EVENTS + 1))).scalars().all()
        return {
            "evidence_level": "rule",
            "range_seconds": [_r(start), _r(end)],
            "truncated": len(rows) > _MAX_EVENTS,
            "events": [
                {"event_id": str(e.id), "type": e.event_type, "time_seconds": _r(e.start_time), "end_seconds": _r(e.end_time), "track_id": e.track_id, "class": e.object_class, "zone": e.zone, "description": e.description}
                for e in rows[:_MAX_EVENTS]
            ],
        }

    async def count_objects(self, camera_id: str | None = None, object_class: str | None = None, **time_args: Any) -> dict[str, Any]:
        source = await self._source(camera_id)
        run = await self._run(source)
        start, end = self._range(source, run, time_args)
        query = select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id, ObjectTrack.first_seen <= end, ObjectTrack.last_seen >= start)
        if object_class:
            query = query.where(ObjectTrack.object_class == object_class)
        tracks = (await self.db.execute(query)).scalars().all()
        snaps = (
            await self.db.execute(select(SceneSnapshot).where(SceneSnapshot.vision_run_id == run.id, SceneSnapshot.timestamp >= start, SceneSnapshot.timestamp <= end))
        ).scalars().all()
        peak: Counter[str] = Counter()
        for s in snaps:
            for cls, n in s.counts.items():
                peak[cls] = max(peak[cls], n)
        return {
            "evidence_level": "tracking",
            "range_seconds": [_r(start), _r(end)],
            "distinct_tracks_by_class": dict(Counter(t.object_class for t in tracks)),
            "max_simultaneously_visible_by_class": {k: v for k, v in peak.items() if not object_class or k == object_class},
            "caveat": "Distinct tracks can over-count: one person who is hidden and reappears may get a new track id.",
        }

    async def get_event_details(self, event_id: str) -> dict[str, Any]:
        try:
            event = await self.db.get(VideoEvent, uuid.UUID(event_id))
        except ValueError:
            event = None
        source = await self.db.get(VideoSourceRecord, event.video_source_id) if event else None
        if event is None or source is None or source.owner_id != self.user_id:
            raise ToolError("Event not found")
        return {
            "event_id": str(event.id),
            "type": event.event_type,
            "evidence_level": event.evidence_level,
            "detector": event.detector,
            "time_seconds": _r(event.start_time),
            "end_seconds": _r(event.end_time),
            "track_id": event.track_id,
            "class": event.object_class,
            "zone": event.zone,
            "confidence": event.confidence,
            "description": event.description,
            "rule": event.event_metadata.get("rule"),
        }

    async def retrieve_video_clip(self, camera_id: str | None = None, start_seconds: float = 0.0, end_seconds: float | None = None) -> dict[str, Any]:
        source = await self._source(camera_id)
        duration = source.source_metadata.get("duration_seconds")
        end = end_seconds if end_seconds is not None else (duration or start_seconds + 30)
        return {"camera_id": str(source.id), "clip": {"start_seconds": _r(start_seconds), "end_seconds": _r(end)}, "note": "The user can jump to this range in the player via the answer's timestamps."}


# JSON-schema declarations handed to the LLM. Kept next to the implementations.
_TIME_PROPS = {
    "start_seconds": {"type": "number", "description": "Range start, seconds from the start of the video"},
    "end_seconds": {"type": "number", "description": "Range end, seconds from the start of the video"},
    "last_seconds": {"type": "number", "description": "Only the final N seconds ('last 10 minutes' = 600). For recorded video 'now' is the end."},
    "start_clock": {"type": "string", "description": "Wall-clock start 'HH:MM' in Kigali time (only if recorded_start_at is known)"},
    "end_clock": {"type": "string", "description": "Wall-clock end 'HH:MM' in Kigali time"},
}
_CAMERA = {"camera_id": {"type": "string", "description": "Camera id or location name; omit for the selected camera"}}
EVENT_TYPES = ["object_appeared", "object_disappeared", "person_entered", "person_exited", "vehicle_entered", "vehicle_exited", "dwell_in_zone", "loitering", "crowd_detected"]
CLASSES = ["person", "bicycle", "car", "motorcycle", "bus", "truck", "backpack", "handbag", "suitcase"]

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": "list_cameras", "description": "List the user's cameras/videos with names and locations.", "parameters": {"type": "object", "properties": {}}},
    {"name": "get_camera_status", "description": "Status of a camera: duration, whether local analysis finished, configured zones, recording start time.", "parameters": {"type": "object", "properties": _CAMERA}},
    {
        "name": "get_current_objects",
        "description": "Which tracked objects are in view at a moment (default: 'now' = end of a recorded video), with how long each has been visible. Evidence: tracking.",
        "parameters": {"type": "object", "properties": {**_CAMERA, "at_seconds": {"type": "number", "description": "Moment in the video, seconds"}}},
    },
    {
        "name": "get_recent_events",
        "description": "Rule-based events in a time range: appearances/disappearances, zone or tripwire entries/exits, dwell, loitering, crowds. Evidence: rule.",
        "parameters": {"type": "object", "properties": {**_CAMERA, **_TIME_PROPS, "event_types": {"type": "array", "items": {"type": "string", "enum": EVENT_TYPES}}, "object_class": {"type": "string", "enum": CLASSES}}},
    },
    {
        "name": "count_objects",
        "description": "Count objects in a time range: distinct tracks per class and the maximum visible at the same time. Evidence: tracking.",
        "parameters": {"type": "object", "properties": {**_CAMERA, **_TIME_PROPS, "object_class": {"type": "string", "enum": CLASSES}}},
    },
    {"name": "get_event_details", "description": "Full details and the rule behind one event.", "parameters": {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]}},
    {
        "name": "retrieve_video_clip",
        "description": "Reference a time range of the video so the user can watch it.",
        "parameters": {"type": "object", "properties": {**_CAMERA, "start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}}, "required": ["start_seconds"]},
    },
    {
        "name": "analyze_video_clip",
        "description": (
            "ESCALATION: send the video (or a time range of it) to a vision-language model for deep understanding: appearance, clothing, colours, "
            "what objects people carry, actions and interactions, anything not covered by local tracking. Costs more; use when local tools cannot answer "
            "or local analysis is unavailable. Evidence: model_interpretation."
        ),
        "parameters": {
            "type": "object",
            "properties": {**_CAMERA, "question": {"type": "string", "description": "What to look for, in English"}, "start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}},
            "required": ["question"],
        },
    },
    {
        "name": "final_answer",
        "description": "Give the final answer to the user. Always finish with this.",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "Natural spoken answer in the user's language, 1-3 sentences, no markdown"},
                "confidence": {"type": "number", "description": "0-1: how well the evidence supports the answer"},
                "insufficient_evidence": {"type": "boolean"},
                "timestamps": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}, "label": {"type": "string"}}, "required": ["start_seconds"]},
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "timestamp_seconds": {"type": "number"},
                            "level": {"type": "string", "enum": ["detection", "tracking", "rule", "model_interpretation"]},
                        },
                        "required": ["description", "level"],
                    },
                },
            },
            "required": ["answer", "confidence", "insufficient_evidence"],
        },
    },
]
