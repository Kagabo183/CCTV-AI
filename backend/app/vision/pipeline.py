"""Detector -> Tracker -> Event engine over a video file.

Produces everything the conversational agent queries later:
  * events       (rule-based, with evidence)
  * tracks       (one summary per track id: class, first/last seen, confidence)
  * snapshots    (once per `snapshot_interval` s: which tracks/classes are in view)
  * stats        (throughput, latency, GPU memory, CPU) for benchmarking

Frames are sampled at `sample_fps` (CCTV rarely needs every frame). Timestamps
are seconds from the start of the media.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.vision import frame_source
from app.vision.detectors import ObjectDetector
from app.vision.events import EventEngine, SceneConfig
from app.vision.trackers import ObjectTracker
from app.vision.types import UNKNOWN, TrackedObject, VisionEvent, resolve_label


@dataclass
class TrackSummary:
    track_id: int
    classes: Counter = field(default_factory=Counter)
    first_seen: float = 0.0
    last_seen: float = 0.0
    frames: int = 0
    confidence_sum: float = 0.0
    max_confidence: float = 0.0
    first_bbox: tuple[float, ...] = ()
    last_bbox: tuple[float, ...] = ()

    @property
    def object_class(self) -> str:
        """The most frequent raw detector class (may be wrong: see resolved())."""
        return self.classes.most_common(1)[0][0]

    def resolved(self, confirm_confidence: float, min_class_share: float) -> tuple[str, str, bool]:
        """(label, candidate, uncertain): 'unknown' unless confident and stable."""
        return resolve_label(dict(self.classes), self.mean_confidence, confirm_confidence=confirm_confidence, min_class_share=min_class_share)

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / max(self.frames, 1)


@dataclass
class Snapshot:
    timestamp: float
    counts: dict[str, int]
    track_ids: list[int]


@dataclass
class VisionRunResult:
    events: list[VisionEvent]
    tracks: dict[int, TrackSummary]
    snapshots: list[Snapshot]
    stats: dict[str, Any]
    # Per processed frame: {"t": seconds, "o": [[track_id, class, conf, x1, y1, x2, y2], ...]}.
    # Lets the UI draw the tracker's boxes over the original video.
    frames: list[dict[str, Any]] = field(default_factory=list)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values), q))


class VisionPipeline:
    def __init__(
        self,
        detector: ObjectDetector,
        tracker: ObjectTracker,
        scene: SceneConfig | None = None,
        *,
        sample_fps: float = 10.0,
        snapshot_interval: float = 1.0,
    ) -> None:
        self.detector = detector
        self.tracker = tracker
        self.scene = scene or SceneConfig()
        self.sample_fps = sample_fps
        self.snapshot_interval = snapshot_interval

    def run(
        self,
        path: Path | str,
        *,
        annotate_to: Path | None = None,
        progress: Callable[[float], None] | None = None,
        max_seconds: float | None = None,
    ) -> VisionRunResult:
        import psutil

        try:
            import torch

            cuda = torch.cuda.is_available()
            if cuda:
                torch.cuda.reset_peak_memory_stats()
        except ImportError:  # pragma: no cover
            cuda = False

        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise ValueError("Could not open the video for local analysis")
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        if max_seconds is not None and total_frames:
            total_frames = min(total_frames, int(max_seconds * src_fps))  # progress over the part we analyse
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        step = max(1, round(src_fps / self.sample_fps))
        effective_fps = src_fps / step

        self.tracker.reset()
        engine = EventEngine(self.scene)
        confirm = self.scene.rules.confirm_confidence
        writer = _open_writer(annotate_to, effective_fps, (width, height)) if annotate_to else None

        events: list[VisionEvent] = []
        frames: list[dict[str, Any]] = []
        tracks: dict[int, TrackSummary] = {}
        snapshots: list[Snapshot] = []
        det_latency: list[float] = []
        trk_latency: list[float] = []
        det_by_class: dict[str, list[float]] = defaultdict(list)
        peak: dict[str, tuple[int, float]] = {}
        next_snapshot = 0.0
        processed = 0
        timestamp = 0.0

        process = psutil.Process()
        process.cpu_percent(None)
        started = time.perf_counter()
        end_frame = int(max_seconds * src_fps) + 1 if max_seconds is not None else None
        remote = str(path).startswith(("http://", "https://"))
        if remote and total_frames > src_fps * 120:
            # online stream longer than 2 minutes: several connections read segments in parallel
            cap.release()
            source = frame_source.parallel(str(path), src_fps, step, min(total_frames, end_frame or total_frames))
        else:
            source = frame_source.sequential(cap, step, end_frame)
        try:
            for frame_index, frame in source:
                timestamp = frame_index / src_fps

                t0 = time.perf_counter()
                detections = self.detector.detect(frame)
                t1 = time.perf_counter()
                tracked = self.tracker.update(detections, frame, frame_index, timestamp)
                t2 = time.perf_counter()
                det_latency.append((t1 - t0) * 1000)
                trk_latency.append((t2 - t1) * 1000)
                for d in detections:
                    det_by_class[d.class_name].append(d.confidence)

                events += engine.process(tracked, timestamp, (height, width))
                frames.append({"t": round(timestamp, 3), "o": [[o.track_id, o.class_name, round(o.confidence, 2), *(round(v, 1) for v in o.bbox)] for o in tracked]})
                _summarise(tracks, tracked)
                labels = Counter(o.class_name if o.confidence >= confirm else UNKNOWN for o in tracked)
                for cls, n in labels.items():
                    if n > peak.get(cls, (0, 0.0))[0]:
                        peak[cls] = (n, round(timestamp, 2))
                if timestamp >= next_snapshot:
                    snapshots.append(Snapshot(round(timestamp, 2), dict(labels), sorted(o.track_id for o in tracked)))
                    next_snapshot = timestamp + self.snapshot_interval
                if writer is not None:
                    writer.write(annotate(frame, tracked, self.scene, timestamp))

                processed += 1
                if progress and total_frames and processed % 25 == 0:
                    progress(min((frame_index + 1) / total_frames, 0.999))
        finally:
            source.close()
            cap.release()
            if writer is not None:
                writer.release()

        wall = time.perf_counter() - started
        events += engine.finish(timestamp)
        events.sort(key=lambda e: e.timestamp)
        per_frame = [d + t for d, t in zip(det_latency, trk_latency)]
        rules = self.scene.rules
        resolved = [t.resolved(rules.confirm_confidence, rules.min_class_share) for t in tracks.values()]
        stats = {
            "detector": self.detector.name,
            "weights": self.detector.weights,
            "model": self.detector.info(),
            "confirm_confidence": rules.confirm_confidence,
            "tracker": self.tracker.name,
            "source_fps": round(src_fps, 2),
            "sample_fps": round(effective_fps, 2),
            "resolution": [width, height],
            "duration_seconds": round(timestamp, 2),
            "frames_processed": processed,
            "wall_seconds": round(wall, 2),
            "processing_fps": round(processed / wall, 1) if wall else 0.0,
            "realtime_factor": round((timestamp / wall), 1) if wall else 0.0,
            "detect_ms_p50": round(_percentile(det_latency, 50), 1),
            "detect_ms_p95": round(_percentile(det_latency, 95), 1),
            "track_ms_p50": round(_percentile(trk_latency, 50), 2),
            "frame_ms_p95": round(_percentile(per_frame, 95), 1),
            "cpu_percent_avg": round(process.cpu_percent(None) / (psutil.cpu_count() or 1), 1),
            "gpu_peak_mb": round(torch.cuda.max_memory_allocated() / 2**20) if cuda else None,
            "detections": {k: {"count": len(v), "mean_confidence": round(statistics.fmean(v), 3)} for k, v in sorted(det_by_class.items())},
            "tracks": {
                "total": len(tracks),
                "by_class": dict(Counter(label for label, _, _ in resolved)),
                "raw_by_class": dict(Counter(t.object_class for t in tracks.values())),
                "uncertain": sum(1 for _, _, u in resolved if u),
                "short_lived": sum(1 for t in tracks.values() if t.last_seen - t.first_seen < 1.0),
                "mean_seconds": round(statistics.fmean([t.last_seen - t.first_seen for t in tracks.values()]), 1) if tracks else 0.0,
            },
            "events": dict(Counter(e.event_type for e in events)),
            # most objects of each class in view in one processed frame, and when (confident detections;
            # the rest are counted as "unknown")
            "max_simultaneous": {cls: {"count": n, "at": t} for cls, (n, t) in sorted(peak.items(), key=lambda kv: -kv[1][0])},
        }
        if progress:
            progress(1.0)
        return VisionRunResult(events=events, tracks=tracks, snapshots=snapshots, stats=stats, frames=frames)


def _summarise(tracks: dict[int, TrackSummary], tracked: list[TrackedObject]) -> None:
    for obj in tracked:
        s = tracks.get(obj.track_id)
        if s is None:
            s = tracks[obj.track_id] = TrackSummary(obj.track_id, first_seen=obj.timestamp, first_bbox=obj.bbox)
        s.classes[obj.class_name] += 1
        s.last_seen = obj.timestamp
        s.frames += 1
        s.confidence_sum += obj.confidence
        s.max_confidence = max(s.max_confidence, obj.confidence)
        s.last_bbox = obj.bbox


# --- annotation -----------------------------------------------------------------

_COLORS = {"person": (80, 200, 120), "car": (255, 170, 60), "truck": (255, 120, 60), "bus": (255, 90, 90), "motorcycle": (220, 120, 255), "bicycle": (60, 200, 255)}


def _open_writer(path: Path, fps: float, size: tuple[int, int]) -> cv2.VideoWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mp4v is always available in OpenCV builds. H.264 (browser-playable) needs an
    # OpenH264/ffmpeg install; converting afterwards is left to tooling.
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    if not writer.isOpened():
        raise RuntimeError("No usable video codec for annotation output")
    return writer


def annotate(frame: np.ndarray, tracked: list[TrackedObject], scene: SceneConfig, timestamp: float) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    for zone in scene.zones:
        pts = np.array([[int(x * w), int(y * h)] for x, y in zone.polygon], dtype=np.int32)
        cv2.polylines(out, [pts], True, (0, 220, 255), 2)
        cv2.putText(out, zone.name, tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1, cv2.LINE_AA)
    for line in scene.lines:
        p1, p2 = (int(line.p1[0] * w), int(line.p1[1] * h)), (int(line.p2[0] * w), int(line.p2[1] * h))
        cv2.line(out, p1, p2, (0, 0, 255), 2)
        cv2.putText(out, line.name, p1, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
    for obj in tracked:
        color = _COLORS.get(obj.class_name, (200, 200, 200))
        x1, y1, x2, y2 = map(int, obj.bbox)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f"#{obj.track_id} {obj.class_name} {obj.confidence:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.rectangle(out, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, y1), color, -1)
        cv2.putText(out, label, (x1 + 2, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
    m, s = divmod(timestamp, 60)
    cv2.putText(out, f"{int(m):02d}:{s:05.2f}", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return out
