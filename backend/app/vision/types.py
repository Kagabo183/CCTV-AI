"""Data types of the local perception layer.

Levels of evidence are kept separate on purpose, from least to most interpretive:

  detection            one frame: "a person-shaped box with 0.91 confidence"
  tracking             across frames: "the same box identity #17 for 42 s"
  rule                 event engine: "track #17's feet crossed into zone 'gate'"
  model_interpretation a vision-language model's description (Gemini/VLM)

Nothing in the local layer claims intent or behaviour ("stole", "suspicious").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EvidenceLevel(StrEnum):
    DETECTION = "detection"
    TRACKING = "tracking"
    RULE = "rule"
    MODEL_INTERPRETATION = "model_interpretation"


# COCO classes that matter for CCTV. Detectors are restricted to these by default.
CCTV_CLASSES = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    24: "backpack",
    26: "handbag",
    28: "suitcase",
}
VEHICLE_CLASSES = frozenset({"bicycle", "car", "motorcycle", "bus", "truck"})


@dataclass(frozen=True)
class Detection:
    class_id: int
    class_name: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels


@dataclass(frozen=True)
class TrackedObject:
    track_id: int
    class_name: str
    confidence: float
    bbox: tuple[float, float, float, float]
    frame_number: int
    timestamp: float  # seconds from the start of the media

    @property
    def anchor(self) -> tuple[float, float]:
        """Bottom-centre of the box: where a person stands / a vehicle touches the ground."""
        x1, _, x2, y2 = self.bbox
        return ((x1 + x2) / 2, y2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "class": self.class_name,
            "confidence": round(self.confidence, 3),
            "bbox": [round(v, 1) for v in self.bbox],
            "timestamp": round(self.timestamp, 3),
            "frame_number": self.frame_number,
        }


@dataclass
class VisionEvent:
    """An event produced by the rule engine from tracking data."""

    event_type: str
    timestamp: float
    description: str
    confidence: float | None = None
    track_id: int | None = None
    object_class: str | None = None
    zone: str | None = None
    end_timestamp: float | None = None
    evidence_level: EvidenceLevel = EvidenceLevel.RULE
    metadata: dict[str, Any] = field(default_factory=dict)
