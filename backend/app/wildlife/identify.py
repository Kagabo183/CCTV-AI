"""Species identification for detected animals, without forcing a species.

Rules (chosen from the documentary benchmark, docs/WILDLIFE_BENCHMARK.md):

1. SpeciesNet at species level, not a domestic animal, score >= min_score  -> the species (certain).
2. SpeciesNet says a domestic animal (e.g. "domestic cattle")               -> uncertain. In the benchmark
   SpeciesNet called rhinos and hippos "domestic cattle" with up to 0.997 confidence, so in wildlife mode
   a domestic label is shown as a possibility, never as fact.
3. SpeciesNet rolled up (genus / family / order / "animal" / "mammal")        -> uncertain; the display names
   the best candidate: SpeciesNet's own top species if its score is >= candidate_score, else the
   African-wildlife specialist's class for that box (if any), else just "animal".
4. The specialist (YOLO trained on the African Wildlife dataset: buffalo, elephant, rhino, zebra) is only
   ever a second opinion for uncertain cases: it also called hippos "rhino" in the benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.wildlife.species import SpeciesNetClassifier, SpeciesResult

DOMESTIC = ("domestic ", "cattle", "feral ")
GENERIC = {"animal", "mammal", "bird", "blank", "no cv result", "vehicle", "human"}


@dataclass
class WildlifeLabel:
    species: str | None  # set only when certain
    candidate: str | None  # best guess when uncertain
    score: float
    certain: bool
    source: str  # speciesnet | speciesnet+rollup | specialist | none
    rank: str = "animal"
    scientific: str | None = None
    top5: list[tuple[str, float]] = field(default_factory=list)

    @property
    def display(self) -> str:
        if self.certain and self.species:
            return self.species
        if self.candidate:
            return f"possible {self.candidate}"
        return "animal (species uncertain)"


def _domestic(label: str) -> bool:
    return label.startswith(DOMESTIC) or label in ("domestic cattle", "cattle")


class WildlifeIdentifier:
    def __init__(self, classifier: SpeciesNetClassifier, specialist: Any | None = None, *, min_score: float = 0.5, candidate_score: float = 0.3) -> None:
        self.classifier = classifier
        self.specialist = specialist  # ultralytics model with names like buffalo / elephant / rhino / zebra
        self.min_score = min_score
        self.candidate_score = candidate_score

    def _specialist_labels(self, frame: np.ndarray, boxes: list[tuple[float, float, float, float]]) -> list[tuple[str, float] | None]:
        if self.specialist is None:
            return [None] * len(boxes)
        out: list[tuple[str, float] | None] = []
        h, w = frame.shape[:2]
        for x1, y1, x2, y2 in boxes:
            mx, my = (x2 - x1) * 0.1, (y2 - y1) * 0.1
            crop = frame[int(max(0, y1 - my)):int(min(h, y2 + my)), int(max(0, x1 - mx)):int(min(w, x2 + mx))]
            if crop.size == 0:
                out.append(None)
                continue
            r = self.specialist.predict(crop, imgsz=320, conf=0.5, verbose=False)[0]
            if r.boxes is None or len(r.boxes) == 0:
                out.append(None)
                continue
            i = int(r.boxes.conf.argmax())
            name = r.names[int(r.boxes.cls[i])]
            out.append(({"rhino": "rhinoceros"}.get(name, name), float(r.boxes.conf[i])))
        return out

    def identify(self, frame: np.ndarray, boxes: list[tuple[float, float, float, float]], det_scores: list[float] | None = None) -> list[WildlifeLabel]:
        results = self.classifier.classify(frame, boxes, det_scores)
        second = self._specialist_labels(frame, boxes)
        return [self._decide(r, s) for r, s in zip(results, second)]

    def _decide(self, r: SpeciesResult, second: tuple[str, float] | None) -> WildlifeLabel:
        common = r.label
        if r.rank == "species" and not _domestic(common) and r.score >= self.min_score:
            return WildlifeLabel(common, None, r.score, True, "speciesnet", r.rank, r.scientific, r.top5)
        # uncertain from here on: pick the most useful candidate, never state it as fact
        wild_top = next(((lbl, s) for lbl, s in r.top5 if lbl not in GENERIC and not _domestic(lbl) and s >= self.candidate_score), None)
        if _domestic(common):
            candidate = second[0] if second else (wild_top[0] if wild_top else common)
            score = second[1] if second else (wild_top[1] if wild_top else r.score)
            return WildlifeLabel(None, candidate, score, False, "specialist" if second else "speciesnet", r.rank, r.scientific, r.top5)
        if wild_top:
            return WildlifeLabel(None, wild_top[0], wild_top[1], False, "speciesnet", r.rank, r.scientific, r.top5)
        if second:
            return WildlifeLabel(None, second[0], second[1], False, "specialist", r.rank, r.scientific, r.top5)
        rolled = common if common not in GENERIC else None  # e.g. "equidae family"
        return WildlifeLabel(None, rolled, r.score, False, "speciesnet+rollup", r.rank, r.scientific, r.top5)
