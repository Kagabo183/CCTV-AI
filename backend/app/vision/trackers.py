"""Multi-object trackers: assign stable IDs to detections across frames.

    ObjectTracker
        ├── ByteTrackTracker   (motion/IoU association, two-stage by score)
        └── BoTSORTTracker     (ByteTrack + camera-motion compensation, optional ReID)

Trackers consume Detection lists from any ObjectDetector, so detector and
tracker can be combined freely (VISION_DETECTOR x VISION_TRACKER).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from app.vision.types import Detection, TrackedObject


class ObjectTracker(ABC):
    name: str

    @abstractmethod
    def update(self, detections: list[Detection], frame: np.ndarray, frame_number: int, timestamp: float) -> list[TrackedObject]:
        """Feed one frame's detections, get the currently tracked objects."""

    @abstractmethod
    def reset(self) -> None: ...


class _UltralyticsTracker(ObjectTracker):
    _config_file: str

    def __init__(self, *, processing_fps: float, lost_track_seconds: float = 3.0, overrides: dict[str, Any] | None = None) -> None:
        from ultralytics.utils import IterableSimpleNamespace, YAML
        from ultralytics.utils.checks import check_yaml

        cfg = YAML.load(check_yaml(self._config_file))
        # track_buffer is counted in processed frames; express it in seconds so it
        # behaves the same whatever frame rate we sample at.
        cfg["track_buffer"] = max(1, round(lost_track_seconds * processing_fps))
        cfg.update(overrides or {})
        self._args = IterableSimpleNamespace(**cfg)
        self._names: dict[int, str] = {}
        self.reset()

    def _make(self) -> Any:
        raise NotImplementedError

    def reset(self) -> None:
        self._tracker = self._make()

    def update(self, detections: list[Detection], frame: np.ndarray, frame_number: int, timestamp: float) -> list[TrackedObject]:
        from ultralytics.engine.results import Boxes

        for d in detections:
            self._names[d.class_id] = d.class_name
        data = (
            np.array([[*d.bbox, d.confidence, d.class_id] for d in detections], dtype=np.float32)
            if detections
            else np.zeros((0, 6), dtype=np.float32)
        )
        rows = self._tracker.update(Boxes(data, frame.shape[:2]), frame)
        tracked = []
        for x1, y1, x2, y2, track_id, score, cls, *_ in rows:
            tracked.append(
                TrackedObject(
                    track_id=int(track_id),
                    class_name=self._names.get(int(cls), str(int(cls))),
                    confidence=float(score),
                    bbox=(float(x1), float(y1), float(x2), float(y2)),
                    frame_number=frame_number,
                    timestamp=timestamp,
                )
            )
        return tracked


class ByteTrackTracker(_UltralyticsTracker):
    name = "bytetrack"
    _config_file = "bytetrack.yaml"

    def _make(self) -> Any:
        from ultralytics.trackers import BYTETracker

        return BYTETracker(self._args)


class BoTSORTTracker(_UltralyticsTracker):
    name = "botsort"
    _config_file = "botsort.yaml"

    def _make(self) -> Any:
        from ultralytics.trackers import BOTSORT

        return BOTSORT(self._args)


def build_tracker(name: str, **kwargs: Any) -> ObjectTracker:
    classes = {"bytetrack": ByteTrackTracker, "botsort": BoTSORTTracker}
    if name not in classes:
        raise ValueError(f"Unknown tracker {name!r}; expected one of {sorted(classes)}")
    return classes[name](**kwargs)
