"""Object detectors: pretrained models, no training.

    ObjectDetector
        ├── YOLODetector    (default: yolo26s.pt)
        └── RTDETRDetector  (default: rtdetr-l.pt)

They are alternatives, selected by VISION_DETECTOR. They are never run together.
Weights are downloaded on first use into VISION_WEIGHTS_DIR.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from app.vision.types import CCTV_CLASSES, Detection


class ObjectDetector(ABC):
    name: str
    weights: str

    @abstractmethod
    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Detect objects in one BGR frame."""

    def warmup(self, shape: tuple[int, int, int] = (640, 640, 3)) -> None:
        self.detect(np.zeros(shape, dtype=np.uint8))


class _UltralyticsDetector(ObjectDetector):
    _model_cls: Any

    def __init__(
        self,
        weights: str,
        *,
        weights_dir: Path,
        device: str = "cuda:0",
        confidence: float = 0.25,
        image_size: int = 640,
        classes: dict[int, str] | None = None,
        half: bool = True,
    ) -> None:
        weights_dir.mkdir(parents=True, exist_ok=True)
        path = Path(weights)
        if not path.is_absolute() and path.parent == Path("."):
            path = weights_dir / path  # Ultralytics downloads known assets to this path
        self.weights = path.name
        self.model = self._load(str(path))
        self.device = device
        self.confidence = confidence
        self.image_size = image_size
        self.classes = classes if classes is not None else CCTV_CLASSES
        self.half = half and device.startswith("cuda")

    @staticmethod
    def _load(path: str) -> Any:
        raise NotImplementedError

    def detect(self, frame: np.ndarray) -> list[Detection]:
        result = self.model.predict(
            frame,
            conf=self.confidence,
            imgsz=self.image_size,
            device=self.device,
            quantize=16 if self.half else 32,
            classes=list(self.classes) or None,
            verbose=False,
        )[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.cpu().numpy()
        conf = boxes.conf.cpu().numpy()
        cls = boxes.cls.cpu().numpy().astype(int)
        names = result.names
        return [
            Detection(class_id=int(c), class_name=self.classes.get(int(c), names[int(c)]), confidence=float(s), bbox=tuple(map(float, b)))
            for b, s, c in zip(xyxy, conf, cls)
        ]


class YOLODetector(_UltralyticsDetector):
    name = "yolo"

    @staticmethod
    def _load(path: str) -> Any:
        from ultralytics import YOLO

        return YOLO(path)


class RTDETRDetector(_UltralyticsDetector):
    name = "rtdetr"

    @staticmethod
    def _load(path: str) -> Any:
        from ultralytics import RTDETR

        return RTDETR(path)


DEFAULT_WEIGHTS = {"yolo": "yolo26s.pt", "rtdetr": "rtdetr-l.pt"}


def build_detector(name: str, weights: str | None = None, **kwargs: Any) -> ObjectDetector:
    classes = {"yolo": YOLODetector, "rtdetr": RTDETRDetector}
    if name not in classes:
        raise ValueError(f"Unknown detector {name!r}; expected one of {sorted(classes)}")
    return classes[name](weights or DEFAULT_WEIGHTS[name], **kwargs)
