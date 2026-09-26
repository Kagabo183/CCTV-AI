"""Species classifiers for animal crops.

SpeciesNetClassifier  Google SpeciesNet v4 (~2,000 labels) + its own ensemble logic:
                      geofencing by country (ISO 3166-1 alpha-3, e.g. RWA) and roll-up
                      to genus/family/order when the species is not certain.
SerengetiClassifier   Microsoft AI4G Snapshot Serengeti ResNet-18 (10 classes), benchmark only.

SpeciesNet's package imports yolov5 for its own MegaDetector v5 detector, which we do not
use (MegaDetector V6 runs separately); two stub modules keep that import from failing.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import numpy as np

DEFAULT_SPECIESNET = "kaggle:google/speciesnet/pyTorch/v4.0.3a/1"
_RANKS = ("species", "genus", "family", "order", "class")


@dataclass
class SpeciesResult:
    label: str  # common name, e.g. "hippopotamus", "rhinocerotidae family", "animal"
    score: float
    rank: str  # species | genus | family | order | class | animal | blank | human | vehicle
    source: str  # classifier | rollup | geofence | detector ...
    scientific: str | None = None
    top5: list[tuple[str, float]] = field(default_factory=list)

    @property
    def is_species(self) -> bool:
        return self.rank == "species"


def _stub_yolov5() -> None:
    for name in ("yolov5", "yolov5.utils", "yolov5.utils.augmentations", "yolov5.utils.general"):
        sys.modules.setdefault(name, types.ModuleType(name))
    aug, gen = sys.modules["yolov5.utils.augmentations"], sys.modules["yolov5.utils.general"]
    for attr in ("letterbox",):
        setattr(aug, attr, getattr(aug, attr, None) or (lambda *a, **k: None))
    for attr in ("non_max_suppression", "xyxy2xywhn", "scale_boxes", "scale_coords"):
        setattr(gen, attr, getattr(gen, attr, None) or (lambda *a, **k: None))


def parse_label(label: str) -> tuple[str, str | None, str]:
    """SpeciesNet label 'uuid;class;order;family;genus;species;common' -> (common, scientific, rank)."""
    parts = label.split(";")
    if len(parts) < 7:
        return label, None, "animal"
    _, cls, order, family, genus, species, common = parts[:7]
    common = common.strip().lower()
    if common == "no cv result":  # SpeciesNet's "could not classify"
        return "animal", None, "animal"
    if species:
        return common, f"{genus} {species}".strip(), "species"
    for rank, value in (("genus", genus), ("family", family), ("order", order), ("class", cls)):
        if value:
            return common, value, rank
    return common or "animal", None, common if common in ("blank", "animal", "vehicle") else "animal"


class SpeciesNetClassifier:
    name = "speciesnet"

    def __init__(self, model: str = DEFAULT_SPECIESNET, country: str | None = "RWA", admin1_region: str | None = None,
                 extra_allow: list[str] | None = None) -> None:
        """extra_allow: "genus species" names to allow in `country` although the model's geofence file
        does not list them there (e.g. species reintroduced after the file was compiled)."""
        self.model_name, self.country, self.admin1 = model, country, admin1_region
        self.extra_allow = [a.strip().lower() for a in (extra_allow or []) if a.strip()]
        self._lock = Lock()
        self._clf: Any = None
        self._ens: Any = None

    def _load(self) -> None:
        if self._clf is not None:
            return
        _stub_yolov5()
        from speciesnet import SpeciesNetClassifier as _Clf
        from speciesnet import SpeciesNetEnsemble

        self._clf = _Clf(self.model_name)
        self._ens = SpeciesNetEnsemble(self.model_name, geofence=True)
        if self.country and self.extra_allow:
            for key, rule in self._ens.geofence_map.items():
                parts = key.split(";")
                name = f"{parts[3]} {parts[4]}".strip().lower() if len(parts) >= 5 else ""  # class;order;family;genus;species
                allow = rule.get("allow")
                if name in self.extra_allow and isinstance(allow, dict) and self.country not in allow:
                    allow[self.country] = []  # whole country

    def classify(self, frame_bgr: np.ndarray, boxes: list[tuple[float, float, float, float]], det_scores: list[float] | None = None,
                 country: str | None = "__default__") -> list[SpeciesResult]:
        """One result per box (x1, y1, x2, y2 in pixels). det_scores feed the ensemble's blank/animal logic."""
        if not boxes:
            return []
        _stub_yolov5()
        import PIL.Image
        from speciesnet.ensemble_prediction_combiner import combine_predictions_for_single_item
        from speciesnet.geofence_utils import geofence_animal_classification, roll_up_labels_to_first_matching_level
        from speciesnet.utils import BBox

        country = self.country if country == "__default__" else country
        with self._lock:
            self._load()
            h, w = frame_bgr.shape[:2]
            img = PIL.Image.fromarray(frame_bgr[:, :, ::-1])
            pre = []
            for x1, y1, x2, y2 in boxes:
                x1, y1, x2, y2 = max(0.0, x1), max(0.0, y1), min(float(w), x2), min(float(h), y2)
                pre.append(self._clf.preprocess(img, bboxes=[BBox(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h)]))
            names = [f"box{i}" for i in range(len(boxes))]
            raw = self._clf.batch_predict(names, pre)
            out = []
            for i, r in enumerate(raw):
                cls = r.get("classifications") or {}
                classes, scores = cls.get("classes") or [], cls.get("scores") or []
                if not classes:
                    out.append(SpeciesResult("animal", 0.0, "animal", "failed"))
                    continue
                label, score, source = combine_predictions_for_single_item(
                    classifications={"classes": classes, "scores": scores},
                    detections=[{"label": "animal", "conf": (det_scores[i] if det_scores else 0.9)}],
                    country=country, admin1_region=self.admin1 if country else None,
                    taxonomy_map=self._ens.taxonomy_map, geofence_map=self._ens.geofence_map, enable_geofence=bool(country),
                    geofence_fn=geofence_animal_classification, roll_up_fn=roll_up_labels_to_first_matching_level,
                )
                common, sci, rank = parse_label(label)
                out.append(SpeciesResult(common, float(score), rank, source, sci, [(parse_label(c)[0], round(float(s), 3)) for c, s in zip(classes[:5], scores[:5])]))
            return out


class SerengetiClassifier:
    """AI4G Snapshot Serengeti (PyTorch-Wildlife), loaded without the PyTorch-Wildlife package."""

    name = "serengeti"
    URL = "https://zenodo.org/records/10456813/files/AI4GSnapshotSerengeti.ckpt?download=1"
    CLASSES = ["wildebeest", "guineafowl", "zebra", "buffalo", "gazellethomsons", "gazellegrants", "warthog", "impala", "hyenaspotted", "other"]

    def __init__(self, device: str = "cuda:0") -> None:
        import torch
        from torch import nn
        from torchvision.models.resnet import BasicBlock, ResNet

        class Backbone(ResNet):
            def _forward_impl(self, x):  # type: ignore[no-untyped-def]
                x = self.maxpool(self.relu(self.bn1(self.conv1(x))))
                x = self.avgpool(self.layer4(self.layer3(self.layer2(self.layer1(x)))))
                return torch.flatten(x, 1)

        class Net(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.feature = Backbone(BasicBlock, [2, 2, 2, 2])
                self.classifier = nn.Linear(512, 10)

            def forward(self, x):  # type: ignore[no-untyped-def]
                return self.classifier(self.feature(x))

        state = torch.hub.load_state_dict_from_url(self.URL, map_location="cpu")["state_dict"]
        state = {k.replace("net.", "", 1): v for k, v in state.items() if k.startswith("net.")}
        self.net = Net()
        self.net.load_state_dict(state, strict=True)
        self.net.eval().to(device)
        self.device = device

    def classify(self, frame_bgr: np.ndarray, boxes: list[tuple[float, float, float, float]], *_: Any, **__: Any) -> list[SpeciesResult]:
        import cv2
        import torch

        crops = []
        for x1, y1, x2, y2 in boxes:
            crop = frame_bgr[max(0, int(y1)):int(y2), max(0, int(x1)):int(x2)]
            crop = cv2.resize(crop, (224, 224))[:, :, ::-1].astype(np.float32) / 255.0
            crops.append((crop - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225))
        if not crops:
            return []
        x = torch.from_numpy(np.stack(crops).transpose(0, 3, 1, 2).astype(np.float32)).to(self.device)
        with torch.inference_mode():
            probs = self.net(x).softmax(-1).cpu().numpy()
        out = []
        for p in probs:
            i = int(p.argmax())
            label = self.CLASSES[i]
            out.append(SpeciesResult(label, float(p[i]), "animal" if label == "other" else "species", "classifier",
                                     top5=[(self.CLASSES[j], round(float(p[j]), 3)) for j in p.argsort()[::-1][:5]]))
        return out
