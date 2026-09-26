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


# Vehicle labels across both vocabularies (COCO and Objects365).
VEHICLE_CLASSES = frozenset({
    "bicycle", "car", "motorcycle", "bus", "truck", "van", "suv", "pickup truck", "sports car", "machinery vehicle",
    "tricycle", "scooter", "ambulance", "fire truck", "heavy truck", "train", "boat",
})
# "animal" is MegaDetector's class; the rest are COCO animals (species come from the wildlife classifier)
ANIMAL_CLASSES = {"animal", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}
UNKNOWN = "unknown"


def resolve_label(votes: dict[str, int], mean_confidence: float, *, confirm_confidence: float, min_class_share: float = 0.6) -> tuple[str, str, bool]:
    """Decide what a track IS, honestly.

    Returns (label, candidate_class, uncertain). The label is "unknown" when the
    detector is not confident (mean confidence below confirm_confidence) or keeps
    changing its mind about the class (top class under min_class_share of frames).
    The candidate class is kept so the UI and agent can say "possibly a person".
    """
    if not votes:
        return UNKNOWN, UNKNOWN, True
    candidate, count = max(votes.items(), key=lambda kv: kv[1])
    share = count / sum(votes.values())
    uncertain = mean_confidence < confirm_confidence or share < min_class_share
    return (UNKNOWN if uncertain else candidate), candidate, uncertain


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
