"""Live AI on connected cameras.

One worker per camera with AI enabled. It reads the camera's SUB stream from the media server (never the
camera itself), at LIVE_AI_FPS, and runs only what the camera's AI profile asks for:

    general  (people/vehicles/objects): the configured detector (YOLO26s + tiling) -> ByteTrack -> event engine
    wildlife (animals + species):       MegaDetector V6 -> ByteTrack -> event engine; SpeciesNet per TRACK on
                                        a few crops, re-checked every SPECIES_EVERY seconds (not every frame)

Everything lands in the same tables as recorded videos (a "live" vision run per worker session):
video_events (with occurred_at wall-clock time), scene_snapshots every 10 s, object_tracks when a track ends,
so the assistant's tools work unchanged. The current picture is kept in memory for "what is happening now".
Detectors are shared between cameras: one inference thread per detector collects the frames every camera
submits within BATCH_WAIT and runs them as ONE GPU batch (up to BATCH_MAX), so 16-32 cameras cost a few GPU calls
per round instead of one each. The stream is re-opened with backoff.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)
SPECIES_EVERY = 15.0
SNAPSHOT_EVERY = 10.0

_workers: dict[uuid.UUID, LiveWorker] = {}
BATCH_MAX = 16
BATCH_WAIT = 0.010  # seconds a frame may wait for others to join its batch
# Events kept when a camera's "behaviour" AI is off: arrivals and departures only (no zones, dwell, crowds...)
BASIC_EVENTS = {"object_appeared", "object_disappeared", "animal_entered", "animal_exited"}

_detectors: dict[str, _Batcher] = {}
_detector_lock = threading.Lock()
_writer_task: asyncio.Task[None] | None = None
_out: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()


class _Batcher:
    """Micro-batching front of a shared detector (thread-safe; callers block until their frame is done)."""

    def __init__(self, detector: Any) -> None:
        self.detector = detector
        self.queue: queue.Queue[tuple[Any, Any]] = queue.Queue()
        self.batches = 0
        self.frames = 0
        threading.Thread(target=self._loop, name=f"batch-{detector.name}", daemon=True).start()

    def detect(self, frame: Any) -> list[Any]:
        from concurrent.futures import Future

        fut: Future[list[Any]] = Future()
        self.queue.put((frame, fut))
        return fut.result(timeout=60)

    def _loop(self) -> None:
        while True:
            items = [self.queue.get()]
            deadline = time.monotonic() + BATCH_WAIT
            while len(items) < BATCH_MAX:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    items.append(self.queue.get(timeout=remaining))
                except queue.Empty:
                    break
            try:
                results = self.detector.detect_batch([f for f, _ in items])
                for (_, fut), r in zip(items, results):
                    fut.set_result(r)
            except Exception as exc:  # noqa: BLE001 - every waiting camera gets the error, the thread lives on
                for _, fut in items:
                    if not fut.done():
                        fut.set_exception(exc)
            self.batches += 1
            self.frames += len(items)


def batch_stats() -> dict[str, Any]:
    return {k: {"batches": b.batches, "frames": b.frames, "mean_batch": round(b.frames / b.batches, 2) if b.batches else 0} for k, b in _detectors.items()}


def _shared_detector(kind: str) -> _Batcher:
    with _detector_lock:
        if kind not in _detectors:
            from pathlib import Path

            from app.services.vision import detector_options, resolve_device, weights_for
            from app.vision.detectors import build_detector

            s = get_settings()
            opts = detector_options(s)
            if s.live_ai_tiling == "off":
                opts["tiling"] = None
            det = build_detector(kind, weights_for(kind, s), weights_dir=Path(s.vision_weights_dir), device=resolve_device(s.vision_device),
                                 classes=list(s.vision_classes) or None, **opts)
            det.warmup()
            _detectors[kind] = _Batcher(det)
        return _detectors[kind]


def state(source_id: uuid.UUID) -> dict[str, Any] | None:
    w = _workers.get(source_id)
    return dict(w.state) if w else None


def last_frame(source_id: uuid.UUID, max_age: float = 5.0) -> Any | None:
    """The newest frame the worker decoded (BGR ndarray), if fresh: snapshots without opening the stream again."""
    w = _workers.get(source_id)
    if w is None or w.frame is None or time.time() - w.frame_at > max_age:
        return None
    return w.frame


def workers() -> dict[str, Any]:
    return {str(k): {"running": w.thread.is_alive(), "fps": w.state.get("ai_fps"), "latency_ms": w.state.get("latency_ms"), "detectors": w.detectors,
                     "frames": w.state.get("frames", 0), "reconnects": w.state.get("reconnects", 0), "dropped": w.state.get("dropped", 0)} for k, w in _workers.items()}


class LiveWorker:
    def __init__(self, source_id: uuid.UUID, stream_url: str, profile: dict[str, Any], scene: dict[str, Any], run_ids: dict[str, uuid.UUID]) -> None:
        self.source_id = source_id
        self.url = stream_url
        self.profile = profile
        self.scene = scene
        self.run_ids = run_ids  # detector kind -> live VisionRun id
        self.detectors = [k for k in ("yolo", "wildlife") if k in run_ids]
        self.stop_event = threading.Event()
        self.frame: Any = None
        self.frame_at = 0.0
        self.state: dict[str, Any] = {"status": "starting", "objects": [], "counts": {}, "frames": 0, "reconnects": 0, "dropped": 0}
        self.thread = threading.Thread(target=self._run, name=f"live-{source_id.hex[:8]}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        import cv2

        from app.vision.events import EventEngine, SceneConfig
        from app.vision.pipeline import _summarise
        from app.vision.trackers import build_tracker

        s = get_settings()
        fps = max(0.5, s.live_ai_fps)
        lanes = {}
        for kind in self.detectors:
            scene = SceneConfig.from_dict(self.scene)
            scene.rules.confirm_confidence = s.vision_confirm_confidence
            lanes[kind] = {"det": _shared_detector(kind), "tracker": build_tracker("bytetrack", processing_fps=fps), "engine": EventEngine(scene),
                           "tracks": {}, "collector": None, "verdicts": {}, "classified_at": 0.0, "peak": {}}
            if kind == "wildlife":
                from app.wildlife.service import CropCollector

                lanes[kind]["collector"] = CropCollector(per_track=3, min_gap=2.0)
        backoff = 2.0
        started = time.time()
        next_snapshot = 0.0
        while not self.stop_event.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.state["status"] = "waiting_for_stream"
                self.stop_event.wait(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            self.state["status"] = "running"
            backoff = 2.0
            last = 0.0
            frame_no = 0
            try:
                while not self.stop_event.is_set():
                    if not cap.grab():
                        break
                    now = time.time()
                    if now - last < 1.0 / fps:
                        continue
                    if last and now - last > 2.5 / fps:
                        self.state["dropped"] += 1  # the analysis could not keep up
                    last = now
                    ok, frame = cap.retrieve()
                    if not ok:
                        break
                    frame_no += 1
                    self.frame, self.frame_at = frame, now
                    t = now - started
                    h, w = frame.shape[:2]
                    objects, counts, t0 = [], Counter(), time.perf_counter()
                    for kind, lane in lanes.items():
                        detections = lane["det"].detect(frame)
                        if kind == "wildlife":
                            detections = [d for d in detections if d.class_name == "animal"]
                        else:
                            detections = [d for d in detections if self._wanted(d.class_name)]
                        tracked = lane["tracker"].update(detections, frame, frame_no, t)
                        _summarise(lane["tracks"], tracked)
                        if lane["collector"] is not None:
                            for o in tracked:
                                lane["collector"].offer(o.track_id, t, frame, o.bbox, o.confidence)
                            if now - lane["classified_at"] > SPECIES_EVERY and lane["collector"].crops:
                                lane["classified_at"] = now
                                self._classify(lane)
                        for e in lane["engine"].process(tracked, t, (h, w)):
                            self._emit_event(kind, lane, e, t)
                        for o in tracked:
                            label = self._label(kind, lane, o.track_id, o.class_name)
                            summary = lane["tracks"].get(o.track_id)
                            first = datetime.fromtimestamp(started + summary.first_seen, UTC).isoformat() if summary else None
                            objects.append({"track_id": o.track_id, "label": label, "confidence": round(o.confidence, 2), "bbox": [round(v) for v in o.bbox],
                                            "detector": kind, "first_seen_at": first})
                            if o.confidence >= s.vision_confirm_confidence and not label.startswith(("possible", "animal")) or (kind == "wildlife" and o.confidence >= 0.25):
                                counts[label] += 1
                        for label, n in counts.items():
                            if n > lane["peak"].get(label, (0, 0))[0]:
                                lane["peak"][label] = (n, round(t, 1))
                        ended = [tid for tid in list(lane["tracks"]) if tid not in {o.track_id for o in tracked} and t - lane["tracks"][tid].last_seen > 5]
                        for tid in ended:
                            self._emit_track(kind, lane, lane["tracks"].pop(tid))
                    self.state.update(objects=objects, counts=dict(counts), at=datetime.now(UTC).isoformat(), frames=self.state["frames"] + 1,
                                      latency_ms=round((time.perf_counter() - t0) * 1000, 1), ai_fps=fps, resolution=[w, h], seconds=round(t, 1))
                    if t >= next_snapshot:
                        next_snapshot = t + SNAPSHOT_EVERY
                        for kind, lane in lanes.items():
                            _out.put(("snapshot", {"run_id": self.run_ids[kind], "t": round(t, 2), "counts": dict(counts),
                                                   "track_ids": [o["track_id"] for o in objects if o["detector"] == kind],
                                                   "duration": round(t, 1), "peak": {k: {"count": v[0], "at": v[1]} for k, v in lane["peak"].items()}}))
            except Exception:  # noqa: BLE001
                logger.exception("live AI worker %s", self.source_id)
            finally:
                cap.release()
            if not self.stop_event.is_set():
                self.state["status"] = "reconnecting"
                self.state["reconnects"] += 1
                self.stop_event.wait(backoff)
                backoff = min(60.0, backoff * 2)
        for kind, lane in lanes.items():
            for summary in lane["tracks"].values():
                self._emit_track(kind, lane, summary)
        self.state["status"] = "stopped"

    def _wanted(self, cls: str) -> bool:
        from app.vision.types import ANIMAL_CLASSES, VEHICLE_CLASSES

        p = self.profile
        if cls == "person":
            return p.get("people", True)
        if cls in VEHICLE_CLASSES:
            return p.get("vehicles", True)
        if cls in ANIMAL_CLASSES:
            return not p.get("wildlife", False)  # the wildlife lane owns animals when it runs
        return p.get("objects", True)

    def _label(self, kind: str, lane: dict[str, Any], track_id: int, cls: str) -> str:
        if kind != "wildlife":
            return cls
        v = lane["verdicts"].get(track_id)
        if not v:
            return "animal"
        return v["species"] if v["certain"] else (f"possible {v['candidate']}" if v.get("candidate") else "animal")

    def _classify(self, lane: dict[str, Any]) -> None:
        import cv2
        import numpy as np

        from app.wildlife.service import decide_track, get_identifier

        identifier = get_identifier()
        for tid, crops in list(lane["collector"].crops.items()):
            if tid in lane["verdicts"] and lane["verdicts"][tid].get("crops", 0) >= len(crops):
                continue
            labels = []
            for c in crops:
                img = cv2.imdecode(np.frombuffer(c.jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    hh, ww = img.shape[:2]
                    labels += identifier.identify(img, [(0.0, 0.0, float(ww), float(hh))], [c.confidence])
            lane["verdicts"][tid] = decide_track(labels)

    def _emit_event(self, kind: str, lane: dict[str, Any], e: Any, t: float) -> None:
        if self.profile.get("behavior", True) is False and e.event_type not in BASIC_EVENTS:
            return
        label = self._label(kind, lane, e.track_id, e.object_class or "") if e.track_id is not None else e.object_class
        summary = lane["tracks"].get(e.track_id) if e.track_id is not None else None
        verdict = lane["verdicts"].get(e.track_id) if e.track_id is not None else None
        memory = {**(e.metadata or {}), **({"bbox": [round(v, 1) for v in summary.last_bbox]} if summary and summary.last_bbox else {})}
        if kind == "wildlife":
            memory["species"] = (verdict or {}).get("species") if (verdict or {}).get("certain") else None
            memory["species_candidate"] = (verdict or {}).get("candidate")
        desc = e.description
        if kind == "wildlife" and e.track_id is not None and label != "animal":
            desc = desc.replace(f"animal #{e.track_id}", f"{label} #{e.track_id}")
        _out.put(("event", {"run_id": self.run_ids[kind], "source_id": self.source_id, "event_type": e.event_type, "evidence": e.evidence_level.value,
                            "object_class": (label or "")[:32] or None, "track_id": e.track_id, "zone": e.zone, "description": desc, "start": round(e.timestamp, 2),
                            "end": round(e.end_timestamp, 2) if e.end_timestamp is not None else None, "confidence": e.confidence, "metadata": memory,
                            "occurred_at": datetime.now(UTC) - timedelta(seconds=max(0.0, t - e.timestamp)), "detector": f"live:{kind}"}))

    def _emit_track(self, kind: str, lane: dict[str, Any], summary: Any) -> None:
        verdict = lane["verdicts"].get(summary.track_id)
        _out.put(("track", {"run_id": self.run_ids[kind], "source_id": self.source_id, "track_id": summary.track_id,
                            "object_class": self._label(kind, lane, summary.track_id, summary.object_class) if kind == "wildlife" else summary.object_class,
                            "first_seen": round(summary.first_seen, 2), "last_seen": round(summary.last_seen, 2), "frames": summary.frames,
                            "mean_confidence": round(summary.mean_confidence, 3), "max_confidence": round(summary.max_confidence, 3),
                            "metadata": {"class_votes": dict(summary.classes), "last_bbox": [round(v, 1) for v in summary.last_bbox],
                                         **({"wildlife": {**verdict, "display": self._label(kind, lane, summary.track_id, "animal")}} if verdict else {})}}))


async def _writer() -> None:
    """Drain worker output into the database (events, snapshots, finished tracks, run stats)."""
    from app.db.session import get_sessionmaker
    from app.models import ObjectTrack, SceneSnapshot, VideoEvent, VisionRun

    while True:
        await asyncio.sleep(1.0)
        items = []
        while not _out.empty() and len(items) < 500:
            items.append(_out.get_nowait())
        if not items:
            continue
        try:
            async with get_sessionmaker()() as db:
                for kind, d in items:
                    if kind == "event":
                        db.add(VideoEvent(video_source_id=d["source_id"], vision_run_id=d["run_id"], event_type=d["event_type"], evidence_level=d["evidence"],
                                          object_class=d["object_class"], track_id=d["track_id"], zone=d["zone"], description=d["description"], start_time=d["start"],
                                          end_time=d["end"], occurred_at=d["occurred_at"], confidence=d["confidence"], detector=d["detector"][:32], event_metadata=d["metadata"]))
                    elif kind == "track":
                        db.add(ObjectTrack(vision_run_id=d["run_id"], video_source_id=d["source_id"], track_id=d["track_id"], object_class=d["object_class"][:32],
                                           first_seen=d["first_seen"], last_seen=d["last_seen"], frames=d["frames"], mean_confidence=d["mean_confidence"],
                                           max_confidence=d["max_confidence"], track_metadata=d["metadata"]))
                    elif kind == "snapshot":
                        db.add(SceneSnapshot(vision_run_id=d["run_id"], timestamp=d["t"], counts=d["counts"], track_ids=d["track_ids"]))
                        run = await db.get(VisionRun, d["run_id"])
                        if run is not None:
                            run.duration_seconds = d["duration"]
                            run.stats = {**(run.stats or {}), "live": True, "max_simultaneous": d["peak"], "updated_at": datetime.now(UTC).isoformat()}
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("live AI writer")


def ai_stream_role(streams: list[dict[str, Any]], preference: str) -> str:
    by_role = {s["role"]: s for s in streams}
    if "sub" not in by_role or preference == "main":
        return "main"
    if preference == "sub" or (by_role["sub"].get("height") or 0) >= 720:
        return "sub"
    return "main"


def start(source_id: uuid.UUID) -> None:
    """Start (or restart) live AI for a camera, if enabled."""
    s = get_settings()
    if not s.live_ai_enabled:
        return
    asyncio.get_running_loop().create_task(_start(source_id))


async def _start(source_id: uuid.UUID) -> None:
    global _writer_task
    from app.cameras import media_server
    from app.db.session import get_sessionmaker
    from app.models import VideoSourceRecord, VisionRun
    from app.services.vision import vision_available, wildlife_available

    if not vision_available():
        return
    if _writer_task is None or _writer_task.done():
        _writer_task = asyncio.create_task(_writer())
    stop(source_id)
    async with get_sessionmaker()() as db:
        source = await db.get(VideoSourceRecord, source_id)
        if source is None or source.kind == "nvr":
            return
        profile = source.ai_profile or {}
        kinds = []
        if profile.get("general", True):
            kinds.append("yolo")
        if profile.get("wildlife") and wildlife_available():
            kinds.append("wildlife")
        if not kinds:
            return
        run_ids = {}
        for kind in kinds:
            run = VisionRun(video_source_id=source.id, status="live", detector=kind, weights=kind, tracker="bytetrack",
                            config={"live": True, "fps": get_settings().live_ai_fps}, stats={"live": True}, duration_seconds=0.0)
            db.add(run)
            await db.flush()
            run_ids[kind] = run.id
        await db.commit()
        role = ai_stream_role(source.source_metadata.get("streams", []), get_settings().live_ai_stream)
        path = media_server.gateway_path(source.gateway_id, source.id, role) if source.gateway_id else media_server.camera_path(source.id, role)
        scene = source.scene_config or {}
    worker = LiveWorker(source_id, media_server.internal_rtsp_url(path), profile, scene, run_ids)
    _workers[source_id] = worker
    worker.start()
    logger.info("Live AI started for camera %s (%s, %s stream)", source_id, "+".join(kinds), role)


def stop(source_id: uuid.UUID) -> None:
    w = _workers.pop(source_id, None)
    if w is not None:
        w.stop()


def stop_all() -> None:
    for sid in list(_workers):
        stop(sid)
