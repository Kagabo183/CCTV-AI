"""Object detectors: pretrained models, no training.

    ObjectDetector
        ├── YOLODetector    yolo26s.pt            COCO, 80 classes      (VISION_DETECTOR=yolo)
        ├── YOLODetector    yolo26s-objv1-150.pt  Objects365, 365 classes (VISION_DETECTOR=yolo_o365)
        └── RTDETRDetector  rtdetr-l.pt           COCO, 80 classes      (VISION_DETECTOR=rtdetr)

Detectors return EVERY class their model knows unless VISION_CLASSES restricts
them. They are closed-set: anything outside the vocabulary is either missed or
reported as the most similar known class (a cartoon rabbit standing upright
comes out as "person"). Confidence is kept on every detection so later layers
can treat weak or unstable labels as "unknown" and ask a vision-language model.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.vision.types import Detection

inference_log = logging.getLogger("app.vision.inference")

DEFAULT_WEIGHTS = {"yolo": "yolo26s.pt", "yolo_o365": "yolo26s-objv1-150.pt", "rtdetr": "rtdetr-l.pt", "wildlife": "wildlife/MDV6-yolov10-e-1280.pt"}
MODEL_CARDS = {
    "yolo": {"label": "YOLO26s", "architecture": "YOLO26s", "dataset": "COCO"},
    "yolo_o365": {"label": "YOLO26s-O365", "architecture": "YOLO26s", "dataset": "Objects365"},
    "rtdetr": {"label": "RT-DETR-L", "architecture": "RT-DETR-L", "dataset": "COCO"},
    "wildlife": {"label": "MegaDetector V6", "architecture": "YOLOv10-e", "dataset": "MegaDetector (animal / person / vehicle) + SpeciesNet"},
}
# The wildlife detector runs as benchmarked: full frame at 1280, no tiling.
DETECTOR_OVERRIDES: dict[str, dict[str, object]] = {"wildlife": {"image_size": 1280, "tiling": None}}


@dataclass(frozen=True)
class TilingConfig:
    """Sliced (SAHI-style) inference for small or distant objects.

    The frame is cut into overlapping tiles of ``tile_size`` source pixels. Each
    tile is run at ``tile_image_size`` (larger than tile_size = upscaled, so a
    15 px person becomes ~30 px for the model). Tile boxes are shifted back to
    frame coordinates and merged with the full-frame pass (when ``full_frame``)
    by greedy non-maximum merging per class: boxes that overlap by more than
    ``merge_threshold`` intersection-over-smaller are fused into one.
    """

    tile_size: int = 0  # 0 = auto: 45% of the frame's short side (320 px on 720p, 640 px on 1440p, as benchmarked)
    overlap: float = 0.25
    tile_image_size: int = 640
    full_frame: bool = True
    merge_threshold: float = 0.5
    # Classes kept from tiles. Upscaled tiles make a closed-set model "see" cows, cakes and aeroplanes in
    # night-time texture; tiling was only validated for these (docs/SMALL_OBJECT_BENCHMARK.md). Other
    # classes come from the full-frame pass only. Empty = keep every class from tiles.
    classes: tuple[str, ...] = ("person", "bicycle", "car", "motorcycle", "bus", "truck")

    def windows(self, width: int, height: int) -> list[tuple[int, int, int, int]]:
        size = self.tile_size if self.tile_size > 0 else max(256, round(min(width, height) * 0.45 / 32) * 32)
        step = max(1, int(size * (1 - self.overlap)))

        def starts(length: int) -> list[int]:
            if length <= size:
                return [0]
            out = list(range(0, length - size, step))
            return out + [length - size]  # last tile flush with the edge

        return [(x, y, min(x + size, width), min(y + size, height)) for y in starts(height) for x in starts(width)]


def merge_detections(dets: list[Detection], threshold: float) -> list[Detection]:
    """Greedy non-maximum merging (as SAHI's GREEDYNMM, metric IOS), per class."""
    out: list[Detection] = []
    for cls in {d.class_id for d in dets}:
        group = sorted((d for d in dets if d.class_id == cls), key=lambda d: -d.confidence)
        boxes = np.array([d.bbox for d in group], dtype=np.float32)
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        used = np.zeros(len(group), dtype=bool)
        for i, d in enumerate(group):
            if used[i]:
                continue
            ix1 = np.maximum(boxes[i, 0], boxes[:, 0])
            iy1 = np.maximum(boxes[i, 1], boxes[:, 1])
            ix2 = np.minimum(boxes[i, 2], boxes[:, 2])
            iy2 = np.minimum(boxes[i, 3], boxes[:, 3])
            inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
            ios = inter / np.maximum(np.minimum(areas[i], areas), 1e-6)
            members = np.where(~used & (ios > threshold))[0]
            used[members] = True
            x1, y1 = boxes[members, 0].min(), boxes[members, 1].min()
            x2, y2 = boxes[members, 2].max(), boxes[members, 3].max()
            out.append(Detection(class_id=d.class_id, class_name=d.class_name, confidence=d.confidence, bbox=(float(x1), float(y1), float(x2), float(y2))))
    return out


class ObjectDetector(ABC):
    name: str
    weights: str
    class_names: dict[int, str]

    @abstractmethod
    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Detect objects in one BGR frame."""

    def warmup(self, shape: tuple[int, int, int] = (640, 640, 3)) -> None:
        self.detect(np.zeros(shape, dtype=np.uint8))

    def info(self) -> dict[str, Any]:
        card = MODEL_CARDS.get(self.name, {})
        return {
            "detector": self.name,
            "weights": self.weights,
            **card,
            "num_classes": len(self.class_names),
            "class_filter": None if self.class_filter is None else [self.class_names[i] for i in self.class_filter],
            "confidence": self.confidence,
            "image_size": self.image_size,
            "tiling": None if self.tiling is None else asdict(self.tiling),
        }

    class_filter: list[int] | None = None
    confidence: float = 0.25
    image_size: int = 640
    tiling: TilingConfig | None = None


class _UltralyticsDetector(ObjectDetector):
    def __init__(
        self,
        name: str,
        weights: str,
        *,
        weights_dir: Path,
        device: str = "cuda:0",
        confidence: float = 0.25,
        image_size: int = 640,
        classes: list[str] | None = None,
        half: bool = True,
        tiling: TilingConfig | None = None,
    ) -> None:
        weights_dir.mkdir(parents=True, exist_ok=True)
        path = Path(weights)
        if not path.is_absolute():
            path = weights_dir / path  # Ultralytics downloads known assets to this path
        self.name = name
        self.weights = path.name
        self.model = self._load(str(path))
        self.class_names = {int(k): str(v) for k, v in self.model.names.items()}
        self.device = device
        self.confidence = confidence
        self.image_size = image_size
        self.half = half and device.startswith("cuda")
        self.tiling = tiling
        if classes:
            by_name = {v.lower(): k for k, v in self.class_names.items()}
            unknown = [c for c in classes if c.lower() not in by_name]
            if unknown:
                raise ValueError(f"{self.weights} has no classes named {unknown}")
            self.class_filter = sorted(by_name[c.lower()] for c in classes)
        else:
            self.class_filter = None  # every class the model knows
        inference_log.info("Loaded %s", self.info())
        inference_log.debug("%s classes: %s", self.weights, self.class_names)

    @staticmethod
    def _load(path: str) -> Any:
        raise NotImplementedError

    def _predict(self, images: np.ndarray | list[np.ndarray], image_size: int) -> list[Any]:
        return self.model.predict(
            images,
            conf=self.confidence,
            imgsz=image_size,
            device=self.device,
            quantize=16 if self.half else 32,
            classes=self.class_filter,
            verbose=False,
        )

    def _to_detections(self, result: Any, dx: float = 0.0, dy: float = 0.0) -> list[Detection]:
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.cpu().numpy() + np.array([dx, dy, dx, dy], dtype=np.float32)
        conf = boxes.conf.cpu().numpy()
        cls = boxes.cls.cpu().numpy().astype(int)
        return [
            Detection(class_id=int(c), class_name=self.class_names[int(c)], confidence=float(s), bbox=tuple(map(float, b)))
            for b, s, c in zip(xyxy, conf, cls)
        ]

    def detect(self, frame: np.ndarray) -> list[Detection]:
        tiling = self.tiling
        if tiling is None:
            detections = self._to_detections(self._predict(frame, self.image_size)[0])
        else:
            h, w = frame.shape[:2]
            windows = tiling.windows(w, h)
            detections = []
            if tiling.full_frame and len(windows) > 1:
                detections += self._to_detections(self._predict(frame, self.image_size)[0])
            crops = [np.ascontiguousarray(frame[y1:y2, x1:x2]) for x1, y1, x2, y2 in windows]
            keep = {c.lower() for c in tiling.classes}
            for (x1, y1, _, _), result in zip(windows, self._predict(crops, tiling.tile_image_size)):
                detections += [d for d in self._to_detections(result, x1, y1) if not keep or d.class_name.lower() in keep]
            detections = merge_detections(detections, tiling.merge_threshold)
        if inference_log.isEnabledFor(logging.DEBUG):
            inference_log.debug("%s -> %s", self.weights, [(d.class_id, d.class_name, round(d.confidence, 3)) for d in detections])
        return detections

    def detect_batch(self, frames: list[np.ndarray]) -> list[list[Detection]]:
        """Several frames (e.g. from different live cameras) in one GPU call. Tiled detection stays per frame."""
        if self.tiling is not None or len(frames) == 1:
            return [self.detect(f) for f in frames]
        return [self._to_detections(r) for r in self._predict(frames, self.image_size)]


class YOLODetector(_UltralyticsDetector):
    @staticmethod
    def _load(path: str) -> Any:
        from ultralytics import YOLO

        return YOLO(path)


class RTDETRDetector(_UltralyticsDetector):
    @staticmethod
    def _load(path: str) -> Any:
        from ultralytics import RTDETR

        return RTDETR(path)


def build_detector(name: str, weights: str | None = None, **kwargs: Any) -> ObjectDetector:
    classes = {"yolo": YOLODetector, "yolo_o365": YOLODetector, "rtdetr": RTDETRDetector, "wildlife": YOLODetector}
    kwargs = {**kwargs, **DETECTOR_OVERRIDES.get(name, {})}
    if name not in classes:
        raise ValueError(f"Unknown detector {name!r}; expected one of {sorted(classes)}")
    return classes[name](name, weights or DEFAULT_WEIGHTS[name], **kwargs)
