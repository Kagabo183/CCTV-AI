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

Live cameras (app/cameras): "now" is now. The live AI worker keeps the current picture in memory and
stores events with their wall-clock time (occurred_at) and a scene snapshot every 10 s, so questions like
"what is happening on Camera 3?" or "the last 20 minutes" are answered in Kigali clock time.
"""

from __future__ import annotations

import asyncio

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import CameraShare, ObjectTrack, SceneSnapshot, VideoEvent, VideoSourceRecord, VisionRun
from app.video.sources.live import LIVE_KINDS

KIGALI = ZoneInfo("Africa/Kigali")
_MAX_EVENTS = 60


class ToolError(Exception):
    """Returned to the model as {"error": ...} so it can recover or explain."""


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _wall(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    return _aware(dt).astimezone(KIGALI).strftime("%H:%M:%S")


def _plural(cls: str) -> str:
    return {"person": "people", "bus": "buses", "sheep": "sheep", "skis": "skis"}.get(cls, cls if cls.endswith("s") else cls + "s")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


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
                needle = str(camera_id).lower().strip()
                rows = await self._visible()
                source = (next((s for s in rows if needle == s.name.lower()), None)
                          or next((s for s in rows if needle in (s.location or "").lower() or needle in s.name.lower()), None))
        if source is None or not await self._may_query(source):
            raise ToolError(f"Unknown camera: {camera_id}. Call list_cameras.")
        return source

    async def _visible(self) -> list[VideoSourceRecord]:
        """The user's own sources plus cameras shared with them (AI questions need the ai_query permission)."""
        own = (await self.db.execute(select(VideoSourceRecord).where(VideoSourceRecord.owner_id == self.user_id))).scalars().all()
        ids = [sh.video_source_id for sh in (await self.db.execute(select(CameraShare).where(CameraShare.user_id == self.user_id))).scalars().all()]
        shared = (await self.db.execute(select(VideoSourceRecord).where(or_(VideoSourceRecord.id.in_(ids), VideoSourceRecord.parent_id.in_(ids))))).scalars().all() if ids else []
        return [*own, *[s for s in shared if await self._may_query(s)]]

    async def _may_query(self, source: VideoSourceRecord) -> bool:
        if source.owner_id == self.user_id:
            return True
        from app.cameras.access import ROLE_PERMISSIONS

        target = [source.id, source.parent_id] if source.parent_id else [source.id]
        share = (await self.db.execute(select(CameraShare).where(CameraShare.user_id == self.user_id, CameraShare.video_source_id.in_(target)))).scalars().first()
        return share is not None and "ai_query" in (ROLE_PERMISSIONS.get(share.role, set()) | set(share.permissions or []))

    # -- live cameras ------------------------------------------------------------

    @staticmethod
    def _is_live(source: VideoSourceRecord) -> bool:
        return source.kind in LIVE_KINDS

    async def _live_runs(self, source: VideoSourceRecord, detector: str | None = None) -> list[VisionRun]:
        q = select(VisionRun).where(VisionRun.video_source_id == source.id, VisionRun.status == "live")
        if detector:
            q = q.where(VisionRun.detector == detector)
        return list((await self.db.execute(q.order_by(VisionRun.created_at.desc()))).scalars().all())

    @staticmethod
    def _wall_range(args: dict[str, Any], default_seconds: float) -> tuple[datetime, datetime]:
        """Live time range (UTC) from last_seconds or Kigali clock times; default: the last `default_seconds`."""
        if args.get("start_seconds") is not None or args.get("end_seconds") is not None:
            raise ToolError("This is a live camera: use last_seconds (e.g. 1200 for the last 20 minutes) or start_clock/end_clock, not video seconds.")
        now = datetime.now(UTC)
        start, end = now - timedelta(seconds=float(args.get("last_seconds") or default_seconds)), now
        today = now.astimezone(KIGALI)
        for key in ("start_clock", "end_clock"):
            if args.get(key):
                try:
                    hour, minute = (int(p) for p in str(args[key]).split(":")[:2])
                except ValueError as exc:
                    raise ToolError(f"{key} must be HH:MM") from exc
                t = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if t > today + timedelta(minutes=1):
                    t -= timedelta(days=1)  # "14:00" asked at 09:00 means yesterday afternoon
                if key == "start_clock":
                    start = t.astimezone(UTC)
                else:
                    end = t.astimezone(UTC)
        return (start, end) if start <= end else (end, start)

    async def _live_status(self, source: VideoSourceRecord) -> dict[str, Any]:
        from app.cameras import live, service

        st = live.state(source.id) or {}
        profile = source.ai_profile or {}
        return {"camera_id": str(source.id), "name": source.name, "location": source.location, "source_type": source.kind, "is_live": True,
                "connection": source.connection_state, "last_seen_kigali": _wall(source.last_seen_at) if source.last_seen_at else None,
                "diagnosis": service.health(source.id).get("diagnosis"),
                "live_ai": {"status": st.get("status", "off"), "general": bool(profile.get("general", True)), "wildlife": bool(profile.get("wildlife"))},
                "zones_configured": [z["name"] for z in (source.scene_config or {}).get("zones", [])], "now_kigali": _wall(datetime.now(UTC))}

    async def _live_now(self, source: VideoSourceRecord) -> dict[str, Any]:
        from app.cameras import live

        if source.connection_state not in (None, "online", "connecting"):
            return {"summary": f"{source.name} is not sending video right now (state: {source.connection_state}), so I cannot say what is happening.",
                    "evidence_level": "tracking", "counts": {}}
        st = live.state(source.id)
        if not st or st.get("status") != "running" or not st.get("at"):
            run = (await self._live_runs(source) or [None])[0]
            snap = (await self.db.execute(select(SceneSnapshot).where(SceneSnapshot.vision_run_id == run.id).order_by(SceneSnapshot.timestamp.desc()).limit(1))).scalar_one_or_none() if run else None
            if run is None or snap is None:
                return {"summary": f"Live analysis is not running on {source.name} yet.", "evidence_level": "tracking", "counts": {}}
            at = _aware(run.created_at) + timedelta(seconds=snap.timestamp)
            visible = ", ".join(f"{n} {c}" for c, n in sorted(snap.counts.items(), key=lambda kv: -kv[1])) or "nothing detected"
            return {"summary": f"Last live picture of {source.name}, at {_wall(at)}: {visible}.", "evidence_level": "tracking", "counts": snap.counts, "at_kigali": _wall(at)}
        counts = st.get("counts") or {}
        at = _wall(datetime.fromisoformat(st["at"]))
        uncertain = [o for o in st.get("objects", []) if o["label"] not in counts]
        visible = ", ".join(f"{n} {c}" for c, n in sorted(counts.items(), key=lambda kv: -kv[1])) or "no people, vehicles or animals detected"
        extra = f" Plus {len(uncertain)} {'object' if len(uncertain) == 1 else 'objects'} the detector is not sure about." if uncertain else ""
        return {"summary": f"Right now ({at}) on {source.name}: {visible}.{extra}", "evidence_level": "tracking", "counts": counts, "at_kigali": at,
                "objects": [{"track_id": o["track_id"], "label": o["label"], "confidence": o["confidence"]} for o in st.get("objects", [])][:30]}

    async def _live_events(self, source: VideoSourceRecord, event_types: list[str] | None, object_class: str | None, time_args: dict[str, Any]) -> dict[str, Any]:
        start, end = self._wall_range(time_args, 3600)
        query = select(VideoEvent).where(VideoEvent.video_source_id == source.id, VideoEvent.occurred_at >= start, VideoEvent.occurred_at <= end)
        if event_types:
            query = query.where(VideoEvent.event_type.in_(event_types))
        if object_class:
            query = query.where(VideoEvent.object_class == object_class)
        rows = (await self.db.execute(query.order_by(VideoEvent.occurred_at).limit(_MAX_EVENTS + 1))).scalars().all()
        kinds = Counter(e.event_type for e in rows)
        n = len(rows)
        return {"summary": f"{n}{'+' if n > _MAX_EVENTS else ''} {'event' if n == 1 else 'events'} on {source.name} between {_wall(start)} and {_wall(end)} (Kigali time): "
                           + (", ".join(f"{n} {k.replace('_', ' ')}" for k, n in kinds.most_common()) or "none") + ".",
                "evidence_level": "rule", "range_kigali": [_wall(start), _wall(end)], "truncated": len(rows) > _MAX_EVENTS,
                "events": [{"event_id": str(e.id), "type": e.event_type, "time_kigali": _wall(e.occurred_at), "track_id": e.track_id, "class": e.object_class,
                            "zone": e.zone, "description": e.description} for e in rows[:_MAX_EVENTS]]}

    async def _live_counts(self, source: VideoSourceRecord, object_class: str | None, time_args: dict[str, Any]) -> dict[str, Any]:
        """Peak simultaneous counts from the 10-second snapshots of every live run overlapping the range."""
        start, end = self._wall_range(time_args, 3600)
        peak: Counter[str] = Counter()
        peak_at: dict[str, datetime] = {}
        distinct: Counter[str] = Counter()
        for run in await self._live_runs(source):
            base = _aware(run.created_at)
            if base > end:
                continue
            lo, hi = (start - base).total_seconds(), (end - base).total_seconds()
            snaps = (await self.db.execute(select(SceneSnapshot).where(SceneSnapshot.vision_run_id == run.id, SceneSnapshot.timestamp >= lo, SceneSnapshot.timestamp <= hi))).scalars().all()
            for snap in snaps:
                for cls, n in snap.counts.items():
                    if n > peak[cls]:
                        peak[cls], peak_at[cls] = n, base + timedelta(seconds=snap.timestamp)
            tracks = (await self.db.execute(select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id, ObjectTrack.first_seen <= hi, ObjectTrack.last_seen >= lo))).scalars().all()
            for t in tracks:
                distinct[t.object_class] += 1
        if object_class:
            peak = Counter({k: v for k, v in peak.items() if k == object_class})
        # plain sentences: they are machine-translated to Kinyarwanda, and "at most N" / "track(s)" were not
        parts = [f"The highest number of {_plural(cls)} seen together was {n}, at {_wall(peak_at[cls])}."
                 + (f" In total, {_plural(cls)} were seen about {distinct[cls]} separate times." if distinct.get(cls, 0) > 1 else "")
                 for cls, n in peak.most_common()]
        return {"summary": (f"On {source.name} between {_wall(start)} and {_wall(end)} (Kigali time): " + " ".join(parts)) if parts
                           else f"Nothing detected on {source.name} between {_wall(start)} and {_wall(end)}.",
                "evidence_level": "tracking", "range_kigali": [_wall(start), _wall(end)], "max_simultaneously_visible_by_class": dict(peak)}

    async def _live_wildlife(self, source: VideoSourceRecord, species: str | None, time_args: dict[str, Any]) -> dict[str, Any]:
        """Animals on a live camera: what is visible now, and each species' first/last sighting (Kigali time).

        With the wildlife profile: MegaDetector + SpeciesNet labels ("possible X" / "animal" when not confident).
        Without it: the general detector's animal classes, which are not species-trained (said so in the summary)."""
        from app.cameras import live
        from app.vision.types import ANIMAL_CLASSES

        start, end = self._wall_range(time_args, 3600)
        wildlife = bool(await self._live_runs(source, "wildlife"))
        detector = "wildlife" if wildlife else "yolo"
        spans: dict[str, dict[str, Any]] = {}

        def note(label: str, first: datetime, last: datetime) -> None:
            g = spans.setdefault(label, {"label": label, "animals": 0, "first": first, "last": last})
            g["animals"] += 1
            g["first"], g["last"] = min(g["first"], first), max(g["last"], last)

        for run in await self._live_runs(source, detector):
            base = _aware(run.created_at)
            lo, hi = (start - base).total_seconds(), (end - base).total_seconds()
            rows = (await self.db.execute(select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id, ObjectTrack.first_seen <= hi, ObjectTrack.last_seen >= lo))).scalars().all()
            for t in rows:
                if wildlife or t.object_class in ANIMAL_CLASSES:
                    note(t.object_class, base + timedelta(seconds=t.first_seen), base + timedelta(seconds=t.last_seen))
        now_objects = [o for o in (live.state(source.id) or {}).get("objects", [])
                       if (o.get("detector") == "wildlife" if wildlife else o.get("label") in ANIMAL_CLASSES)]
        now_at = datetime.now(UTC)
        for o in now_objects:  # animals still in view have no finished track yet
            first = datetime.fromisoformat(o["first_seen_at"]) if o.get("first_seen_at") else now_at
            note(o["label"], first, now_at)
        now = Counter(o["label"] for o in now_objects)
        if species:
            needle = species.lower().removesuffix("s")
            spans = {k: v for k, v in spans.items() if needle in k.lower()}
            now = Counter({k: v for k, v in now.items() if needle in k.lower()})
        groups = sorted(spans.values(), key=lambda g: g["first"])
        # "animal" = MegaDetector found an animal but no species was confident: say so, never guess one
        name = lambda k, n=1: ("unknown animal" if k == "animal" else k) if n == 1 else ("unknown animals" if k == "animal" else _plural(k))  # noqa: E731
        seen = "; ".join(f"{name(g['label'])}: first seen at {_wall(g['first'])}, last seen at {_wall(g['last'])}" for g in groups) or "none"
        visible = ", ".join(f"{v} {name(k, v)}" for k, v in now.most_common()) or "none"
        caveat = ("'possible' and 'animal' mean the species classifier was not confident."
                  if wildlife else "Wildlife recognition is off on this camera, so these are the general detector's guesses, not identified species.")
        return {"summary": f"Animals on {source.name}: visible right now: {visible}. Between {_wall(start)} and {_wall(end)} (Kigali time): {seen}. {caveat}",
                "evidence_level": "detection", "visible_now": dict(now), "wildlife_profile": wildlife,
                "species": [{"label": g["label"], "certain": wildlife and not g["label"].startswith(("possible", "animal")), "animals": g["animals"],
                             "first_seen": None, "last_seen": None, "first_seen_kigali": _wall(g["first"]), "last_seen_kigali": _wall(g["last"])} for g in groups]}

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

    async def _wildlife_run(self, source: VideoSourceRecord) -> VisionRun | None:
        runs = (await self.db.execute(select(VisionRun).where(VisionRun.video_source_id == source.id, VisionRun.detector == "wildlife", VisionRun.status == "completed").order_by(VisionRun.created_at.desc()))).scalars().all()
        return runs[0] if runs else None

    # -- tools -----------------------------------------------------------------

    async def list_wildlife(self, camera_id: str | None = None, species: str | None = None, **time_args: Any) -> dict[str, Any]:
        """Animals from the wildlife run (MegaDetector V6 + SpeciesNet), grouped by species, with uncertainty."""
        source = await self._source(camera_id)
        if self._is_live(source):
            return await self._live_wildlife(source, species, time_args)
        run = await self._wildlife_run(source)
        if run is None:
            return {"available": False, "summary": "No wildlife analysis has finished for this video yet, so species are not known."}
        start, end = self._range(source, run, time_args)
        rows = (await self.db.execute(select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id, ObjectTrack.first_seen <= end, ObjectTrack.last_seen >= start).order_by(ObjectTrack.first_seen))).scalars().all()
        animals = []
        width = (run.stats.get("resolution") or [0, 0])[0] or 0
        for t in rows:
            w = (t.track_metadata or {}).get("wildlife")
            if not w:
                continue
            label = w["species"] if w.get("certain") else (w.get("candidate") or "unidentified animal")
            box = (t.track_metadata or {}).get("last_bbox") or []
            position = None
            if width and len(box) == 4:
                cx = (box[0] + box[2]) / 2 / width
                position = "left" if cx < 0.33 else "right" if cx > 0.66 else "centre"
            animals.append({"animal_id": t.track_id, "label": label, "certain": bool(w.get("certain")), "confidence": w.get("score"), "scientific": w.get("scientific"),
                            "first_seen": _r(t.first_seen), "last_seen": _r(t.last_seen), "first_seen_clock": _clock(t.first_seen), "last_seen_clock": _clock(t.last_seen), "position_in_frame": position})
        if species:
            needle = species.lower().replace("hippo ", "hippopotamus ").strip()
            needle = {"hippo": "hippopotamus", "rhino": "rhinoceros"}.get(needle, needle)
            animals = [a for a in animals if needle in a["label"].lower() or a["label"].lower() in needle]
        peaks = {cls: v["count"] for cls, v in (run.stats.get("max_simultaneous") or {}).items()}
        groups: dict[str, dict[str, Any]] = {}
        for a in animals:
            key = a["label"] if a["certain"] else f"possible {a['label']}" if a["label"] != "unidentified animal" else a["label"]
            g = groups.setdefault(key, {"label": key, "certain": a["certain"], "animals": 0, "first_seen": a["first_seen"], "last_seen": a["last_seen"]})
            g["animals"] += 1
            g["first_seen"], g["last_seen"] = min(g["first_seen"], a["first_seen"]), max(g["last_seen"], a["last_seen"])
        parts = []
        for g in sorted(groups.values(), key=lambda g: (not g["certain"], -g["animals"])):
            at_once = peaks.get(g["label"]) if g["certain"] else None
            parts.append(f"{g['label']}: {g['animals']} separate animal(s)" + (f", at most {int(at_once)} at the same time" if at_once else "")
                         + f", seen {_clock(g['first_seen'])}–{_clock(g['last_seen'])}" + ("" if g["certain"] else " (species uncertain)"))
        identified = sum(1 for a in animals if a["certain"])
        all_at_once = max((v for k, v in peaks.items() if k not in ("person", "vehicle")), default=0)
        total = (f"In total {len(animals)} separate animal(s): {identified} identified to species, {len(animals) - identified} uncertain"
                 + (f"; the most animals of one species at the same time was {int(all_at_once)}" if all_at_once else "") + ". ") if not species else ""
        summary = (f"Wildlife between {_clock(start)} and {_clock(end)}: {total}" + "; ".join(parts) + ". "
                   "Species come from SpeciesNet on cropped animals; 'possible' and 'unidentified' mean the classifier was not confident.") if parts else \
                  f"No {species or 'animals'} found between {_clock(start)} and {_clock(end)}."
        return {"summary": summary, "evidence_level": "detection", "range_seconds": [_r(start), _r(end)], "species": list(groups.values()), "animals": animals[:25],
                "analysed_seconds": _r(run.duration_seconds)}

    async def list_cameras(self) -> dict[str, Any]:
        rows = [s for s in await self._visible() if s.kind != "nvr"]
        return {"cameras": [{"camera_id": str(s.id), "name": s.name, "location": s.location, "live": self._is_live(s),
                             **({"state": s.connection_state} if self._is_live(s) else {}), "selected": s.id == self.default_source_id} for s in rows]}

    async def get_camera_status(self, camera_id: str | None = None) -> dict[str, Any]:
        source = await self._source(camera_id)
        if self._is_live(source):
            return await self._live_status(source)
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
        if self._is_live(source):
            return await self._live_now(source)
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
        visible = ", ".join(f"{n} {cls}" for cls, n in sorted(snap.counts.items(), key=lambda kv: -kv[1])) or "nothing"
        return {
            "summary": f"At {_clock(snap.timestamp)}{' (the end of the recording)' if at_seconds is None else ''}: {visible} in view. "
                       + ("For 'how many at the same time' over the whole video use count_objects instead." if at_seconds is None else ""),
            "evidence_level": "tracking",
            "at_seconds": snap.timestamp,
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
        if self._is_live(source):
            return await self._live_events(source, event_types, object_class, time_args)
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
        kinds = Counter(e.event_type for e in rows)
        return {
            "summary": f"{len(rows)}{'+' if len(rows) > _MAX_EVENTS else ''} event(s) between {_clock(start)} and {_clock(end)}: "
                       + (", ".join(f"{n} {k.replace('_', ' ')}" for k, n in kinds.most_common()) or "none") + ".",
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
        if self._is_live(source):
            return await self._live_counts(source, object_class, time_args)
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
        distinct = dict(Counter(t.object_class for t in tracks))
        at_once = {k: v for k, v in peak.items() if not object_class or k == object_class}
        parts = [
            f"{cls}: at most {at_once.get(cls, 0)} visible at the same time, {n} separate track(s) in total"
            for cls, n in sorted(distinct.items(), key=lambda kv: -kv[1]) if cls != "unknown"
        ]
        if distinct.get("unknown"):
            parts.append(f"{distinct['unknown']} object(s) the detector could not identify")
        return {
            "summary": (f"Between {_clock(start)} and {_clock(end)}: " + "; ".join(parts) + ". "
                        "'At the same time' is the reliable count; separate tracks over-count objects that leave and come back.") if parts else f"No objects between {_clock(start)} and {_clock(end)}.",
            "evidence_level": "tracking",
            "range_seconds": [_r(start), _r(end)],
            "distinct_tracks_by_class": distinct,
            "max_simultaneously_visible_by_class": at_once,
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

    async def list_uncertain_objects(self, camera_id: str | None = None) -> dict[str, Any]:
        """Tracks the detector could not confidently classify (label 'unknown')."""
        source = await self._source(camera_id)
        run = await self._run(source)
        rows = (
            await self.db.execute(select(ObjectTrack).where(ObjectTrack.vision_run_id == run.id, ObjectTrack.object_class == "unknown").order_by(ObjectTrack.first_seen))
        ).scalars().all()
        return {
            "evidence_level": "detection",
            "note": "The detector was not confident about these. Use analyze_video_clip on their time window to find out what they are.",
            "objects": [
                {"track_id": t.track_id, "candidate_class": t.track_metadata.get("candidate_class"), "mean_confidence": t.mean_confidence,
                 "class_votes": t.track_metadata.get("class_votes"), "from_seconds": _r(t.first_seen), "to_seconds": _r(t.last_seen)}
                for t in rows[:30]
            ],
            "total": len(rows),
        }

    async def local_media_path(self, camera_id: str | None = None) -> "Path | str | None":
        """The stored video file (or, for online videos, the direct stream URL) for frame-based vision-language models."""
        from pathlib import Path

        try:
            source = await self._source(camera_id)
        except ToolError:
            return None
        s = get_settings()
        if self._is_live(source):
            return await self._live_clip(source)
        stored = source.source_metadata.get("stored_path") or (source.uri if source.kind == "upload" else None)
        if stored:
            return (s.upload_dir / stored).resolve()
        if source.kind == "local":
            return (s.local_video_dir / source.uri).resolve()
        from app.video import streaming

        if streaming.is_streamed(source.source_metadata):
            try:
                return (await asyncio.to_thread(streaming.resolve, source.uri, min(720, s.video_import_max_height))).url
            except Exception:  # noqa: BLE001 - the VLM then reports it cannot see the video
                return None
        return None

    @staticmethod
    async def _live_clip(source: VideoSourceRecord, seconds: float = 8.0) -> "Path | None":
        """A few seconds of the live camera, recorded now, for the vision-language model (kept 10 minutes)."""
        import time as _time
        from pathlib import Path

        from app.cameras import media_server
        from app.video import ffmpeg

        folder = Path(get_settings().video_work_dir) / "live-clips"
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.mp4"):
            if _time.time() - old.stat().st_mtime > 600:
                old.unlink(missing_ok=True)
        roles = {st["role"] for st in source.source_metadata.get("streams", [])}
        role = "sub" if "sub" in roles else "main"
        path = media_server.gateway_path(source.gateway_id, source.id, role) if source.gateway_id else media_server.camera_path(source.id, role)
        dst = folder / f"{source.id.hex}-{int(_time.time())}.mp4"
        try:
            await asyncio.to_thread(ffmpeg.capture, media_server.internal_rtsp_url(path), dst, seconds=seconds, max_height=720)
        except Exception:  # noqa: BLE001 - the VLM then says it cannot see the camera
            return None
        return dst

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
EVENT_TYPES = ["object_appeared", "object_disappeared", "person_entered", "person_exited", "vehicle_entered", "vehicle_exited", "dwell_in_zone", "loitering", "crowd_detected", "animal_entered", "animal_exited", "animal_group_detected", "animal_approaching_restricted_area", "animal_in_restricted_area", "wildlife_near_infrastructure", "unusual_movement"]
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
        "name": "list_wildlife",
        "description": ("Animals seen by the dedicated wildlife detector with their species (or 'possible X' when uncertain): per species how many "
                        "animals, how many at the same time, first/last seen, and per animal its position in the frame. Use for any question about "
                        "animals, species, where an animal is or when it arrived. Filter with species (e.g. 'hippopotamus') and a time range."),
        "parameters": {"type": "object", "properties": {**_CAMERA, **_TIME_PROPS, "species": {"type": "string", "description": "e.g. hippopotamus, zebra, elephant"}}},
    },
    {
        "name": "list_uncertain_objects",
        "description": "Objects the detector saw but could not confidently classify (label 'unknown', with candidate class and confidence) and when they appear.",
        "parameters": {"type": "object", "properties": _CAMERA},
    },
    {
        "name": "analyze_video_clip",
        "description": (
            "ESCALATION: send the video (or a time range of it) to a vision-language model for open-ended understanding: what an object or animal is, "
            "appearance, clothing, colours, what people carry, actions, interactions, what changed, anything outside the detector's class list or "
            "labelled 'unknown'. Costs more; use when local tools cannot answer or local analysis is unavailable. Evidence: model_interpretation."
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
