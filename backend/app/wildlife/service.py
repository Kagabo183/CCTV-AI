"""Wildlife run: MegaDetector V6 detections are tracked like any object; species are decided per TRACK.

SpeciesNet does not run on every frame. While the video is processed, CropCollector keeps a few
crops per animal track (at least `min_gap` seconds apart, preferring large views). After the run,
each track's crops are classified and combined into one verdict (species, or "possible X", or
"animal, species uncertain"), which then relabels the track, its boxes, its events and the counts.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import cv2
import numpy as np

from app.vision.types import ANIMAL_CLASSES
from app.wildlife.identify import WildlifeIdentifier, WildlifeLabel

logger = logging.getLogger(__name__)


@dataclass
class _Crop:
    t: float
    area: float
    jpg: bytes
    confidence: float


@dataclass
class CropCollector:
    per_track: int = 4
    min_gap: float = 2.0
    crops: dict[int, list[_Crop]] = field(default_factory=dict)

    def offer(self, track_id: int, t: float, frame: np.ndarray, bbox: tuple[float, ...], confidence: float) -> None:
        x1, y1, x2, y2 = bbox
        area = max(0.0, (x2 - x1) * (y2 - y1))
        kept = self.crops.setdefault(track_id, [])
        if kept and t - kept[-1].t < self.min_gap and not (area > 1.5 * kept[-1].area):
            return
        if len(kept) >= self.per_track:
            smallest = min(range(len(kept)), key=lambda i: kept[i].area)
            if area <= kept[smallest].area:
                return
            kept.pop(smallest)
        h, w = frame.shape[:2]
        mx, my = (x2 - x1) * 0.1, (y2 - y1) * 0.1
        crop = frame[int(max(0, y1 - my)):int(min(h, y2 + my)), int(max(0, x1 - mx)):int(min(w, x2 + mx))]
        if crop.size == 0:
            return
        ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            kept.append(_Crop(t, area, jpg.tobytes(), confidence))


def decide_track(labels: list[WildlifeLabel]) -> dict[str, Any]:
    """Combine a track's crop verdicts. Certain only if most crops agree on the same species."""
    if not labels:
        return {"species": None, "candidate": None, "certain": False, "score": 0.0, "crops": 0}
    sure = Counter(lab.species for lab in labels if lab.certain and lab.species)
    if sure:
        species, n = sure.most_common(1)[0]
        if n / len(labels) >= 0.5:
            scores = [lab.score for lab in labels if lab.species == species]
            sci = next((lab.scientific for lab in labels if lab.species == species), None)
            return {"species": species, "candidate": None, "certain": True, "score": round(sum(scores) / len(scores), 3), "scientific": sci,
                    "crops": len(labels), "votes": dict(sure)}
    guesses = Counter((lab.species if lab.certain else lab.candidate) for lab in labels if (lab.species if lab.certain else lab.candidate))
    candidate = guesses.most_common(1)[0][0] if guesses else None
    scores = [lab.score for lab in labels if (lab.species if lab.certain else lab.candidate) == candidate] if candidate else [0.0]
    return {"species": None, "candidate": candidate, "certain": False, "score": round(max(scores), 3), "crops": len(labels), "votes": dict(guesses)}


def display(verdict: dict[str, Any] | None) -> str:
    if not verdict:
        return "animal"
    if verdict["certain"]:
        return verdict["species"]
    return f"possible {verdict['candidate']}" if verdict.get("candidate") else "animal (species uncertain)"


def apply_species(result: Any, collector: CropCollector, identifier: WildlifeIdentifier, confirm: float) -> dict[int, dict[str, Any]]:
    """Classify each track's crops and relabel tracks, frames, events and counts in `result` (in place)."""
    verdicts: dict[int, dict[str, Any]] = {}
    for track_id, crops in collector.crops.items():
        labels: list[WildlifeLabel] = []
        for c in crops:
            img = cv2.imdecode(np.frombuffer(c.jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            h, w = img.shape[:2]
            labels += identifier.identify(img, [(0.0, 0.0, float(w), float(h))], [c.confidence])
        verdicts[track_id] = decide_track(labels)
    label_of = {tid: (v["species"] if v["certain"] else "animal") for tid, v in verdicts.items()}

    for tid, summary in result.tracks.items():
        if tid in verdicts:
            v = verdicts[tid]
            summary.classes = Counter({label_of[tid]: summary.frames})
            summary.wildlife = {**v, "display": display(v)}
    for f in result.frames:
        for o in f["o"]:
            if o[0] in label_of:
                o[1] = label_of[o[0]]
    for e in result.events:
        if e.track_id in verdicts:
            e.object_class = label_of[e.track_id]
            e.description = re.sub(rf"\b(animal|unidentified object \([^)]*\)) #{e.track_id}\b", f"{display(verdicts[e.track_id])} #{e.track_id}", e.description)
            e.metadata["wildlife"] = verdicts[e.track_id]
    for snap in result.snapshots:
        snap.counts = dict(Counter(label_of.get(tid, "animal") for tid in snap.track_ids))
    peak: dict[str, tuple[int, float]] = {}
    for f in result.frames:
        counts = Counter(o[1] for o in f["o"] if o[2] >= confirm)
        for cls, n in counts.items():
            if n > peak.get(cls, (0, 0.0))[0]:
                peak[cls] = (n, f["t"])
    result.stats["max_simultaneous"] = {cls: {"count": n, "at": t} for cls, (n, t) in sorted(peak.items(), key=lambda kv: -kv[1][0])}
    result.stats["wildlife"] = {
        "species": dict(Counter(display(v) for v in verdicts.values())),
        "tracks_classified": len(verdicts),
        "crops_classified": sum(v.get("crops", 0) for v in verdicts.values()),
    }
    return verdicts


def is_animal(class_name: str) -> bool:
    return class_name in ANIMAL_CLASSES


@lru_cache
def get_identifier() -> WildlifeIdentifier:
    from app.core.config import get_settings
    from app.wildlife.species import SpeciesNetClassifier

    s = get_settings()
    classifier = SpeciesNetClassifier(s.wildlife_speciesnet_model, country=s.wildlife_country or None, extra_allow=s.wildlife_geofence_allow)
    specialist = None
    path = s.vision_weights_dir / s.wildlife_specialist_weights if s.wildlife_specialist_weights else None
    if path is not None and path.exists():
        from ultralytics import YOLO

        specialist = YOLO(str(path))
    return WildlifeIdentifier(classifier, specialist)
