"""Small / distant object detection benchmark on our own footage.

    python scripts/small_object_benchmark.py frames      # extract the ground-truth frames
    python scripts/small_object_benchmark.py candidates  # high-recall candidate boxes + review images
    (a human reviews the images and writes bench/gt/<frame>.gt.json)
    python scripts/small_object_benchmark.py evaluate    # every config x confidence vs the ground truth

Ground truth is hand-verified: every candidate box from a very permissive union of
detectors is checked by eye (kept or rejected), and objects no detector found are
added by hand. Metrics are then computed per configuration and threshold.

Matching: a detection matches an unmatched ground-truth object of the same group
when IoU >= 0.3, or (small objects, where a few pixels change IoU a lot) when its
centre lies inside the ground-truth box. Greedy, highest confidence first.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.vision.detectors import TilingConfig, YOLODetector, merge_detections  # noqa: E402
from app.vision.types import Detection  # noqa: E402

ROOT = Path(__file__).resolve().parents[2] / "bench"
FRAMES, GT, REVIEW = ROOT / "frames", ROOT / "gt", ROOT / "review"
WEIGHTS = Path(__file__).resolve().parents[1] / "models"

VIDEOS = {
    # id: (short name, seconds to sample for ground truth)
    "7783cf88926549e98d4274eb598badb1": ("plaza", [2, 6, 10, 14, 18, 22]),  # overhead night plaza, 1280x720
    "5968beacd89f48729526301167017482": ("aerial", [3, 10, 17, 24]),  # aerial intersection, 2560x1440
}
GROUPS = {"person": {"person"}, "vehicle": {"car", "truck", "bus", "motorcycle"}, "bicycle": {"bicycle"}}
THRESHOLDS = [0.50, 0.35, 0.25, 0.15]


def group_of(name: str) -> str | None:
    return next((g for g, names in GROUPS.items() if name in names), None)


def video_path(source_id: str) -> Path:
    backend = Path(__file__).resolve().parents[1]
    (uri,) = sqlite3.connect(backend / "dev.db").execute("select uri from video_sources where id=?", (source_id,)).fetchone()
    return backend / "uploads" / uri


# ---------------------------------------------------------------- configurations
@dataclass
class Config:
    name: str
    weights: str
    image_size: int
    tiling: dict[str, TilingConfig | None] = field(default_factory=dict)  # per video; None = full frame only

    def detector(self, video: str, confidence: float) -> YOLODetector:
        return YOLODetector("yolo", self.weights, weights_dir=WEIGHTS, confidence=confidence, image_size=self.image_size, tiling=self.tiling.get(video))


def _tiles(plaza: int, aerial: int, size: int = 640) -> dict[str, TilingConfig]:
    # tile_size in source pixels; tiles are run at `size`, so plaza tiles of 320 px are upscaled 2x
    return {"plaza": TilingConfig(tile_size=plaza, overlap=0.25, tile_image_size=size), "aerial": TilingConfig(tile_size=aerial, overlap=0.25, tile_image_size=size)}


CONFIGS = [
    Config("YOLO26s @640", "yolo26s.pt", 640),
    Config("YOLO26s @960", "yolo26s.pt", 960),
    Config("YOLO26s @1280", "yolo26s.pt", 1280),
    Config("YOLO26m @1280", "yolo26m.pt", 1280),
    Config("YOLO26l @1280", "yolo26l.pt", 1280),
    Config("YOLO26s tiled", "yolo26s.pt", 1280, _tiles(320, 640)),
    Config("YOLO26s tiled-lg", "yolo26s.pt", 1280, _tiles(480, 960, 960)),
    Config("YOLO26m tiled", "yolo26m.pt", 1280, _tiles(320, 640)),
    Config("YOLO26s tiled-xs", "yolo26s.pt", 1280, _tiles(240, 480)),
    Config("O365 tiled", "yolo26s-objv1-150.pt", 1280, _tiles(320, 640)),
    # what the platform runs with the shipped .env (auto tile size)
    Config("Platform default", "yolo26s.pt", 1280, {"plaza": TilingConfig(), "aerial": TilingConfig()}),
]


# ---------------------------------------------------------------- frames
def extract() -> None:
    FRAMES.mkdir(parents=True, exist_ok=True)
    for sid, (name, seconds) in VIDEOS.items():
        cap = cv2.VideoCapture(str(video_path(sid)))
        fps = cap.get(cv2.CAP_PROP_FPS)
        for t in seconds:
            cap.set(cv2.CAP_PROP_POS_FRAMES, round(t * fps))
            ok, frame = cap.read()
            assert ok, (name, t)
            cv2.imwrite(str(FRAMES / f"{name}_{t:02d}.png"), frame)
        print(name, cap.get(3), cap.get(4), f"{fps:.2f} fps", seconds)


def frames() -> list[tuple[str, str, np.ndarray]]:
    return [(p.stem, p.stem.split("_")[0], cv2.imread(str(p))) for p in sorted(FRAMES.glob("*.png"))]


# ---------------------------------------------------------------- candidates for human review
def candidates() -> None:
    GT.mkdir(parents=True, exist_ok=True)
    REVIEW.mkdir(parents=True, exist_ok=True)
    union = [c for c in CONFIGS if c.name in ("YOLO26l @1280", "YOLO26s tiled", "YOLO26m tiled")]
    for stem, video, img in frames():
        dets: list[Detection] = []
        for c in union:
            dets += [d for d in c.detector(video, 0.08).detect(img) if group_of(d.class_name)]
        # one candidate per object regardless of which model / class proposed it
        dets = [replace(d, class_id=list(GROUPS).index(group_of(d.class_name))) for d in dets]
        merged = sorted(merge_detections(dets, 0.5), key=lambda d: (d.bbox[1], d.bbox[0]))
        cands = [{"id": i, "group": group_of(d.class_name), "box": [round(v, 1) for v in d.bbox], "conf": round(d.confidence, 2)} for i, d in enumerate(merged)]
        (GT / f"{stem}.candidates.json").write_text(json.dumps(cands, indent=0))
        render_review(stem, img, cands)
        print(stem, len(cands), "candidates")


COLORS = {"person": (60, 220, 60), "vehicle": (255, 160, 40), "bicycle": (40, 200, 255)}


def render_review(stem: str, img: np.ndarray, cands: list[dict], extra: list[dict] | None = None) -> None:
    """Quadrant crops upscaled 2x (720p) or 1x (1440p) with numbered candidate boxes and a 50 px grid."""
    h, w = img.shape[:2]
    scale = 2 if h <= 720 else 1
    for qy in range(2):
        for qx in range(2):
            x0, y0 = qx * w // 2, qy * h // 2
            crop = img[y0 : y0 + h // 2, x0 : x0 + w // 2]
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            for gx in range(0, w // 2, 50):
                cv2.line(crop, (gx * scale, 0), (gx * scale, 6), (255, 255, 255), 1)
                cv2.putText(crop, str(x0 + gx), (gx * scale + 1, 16), cv2.FONT_HERSHEY_PLAIN, 0.8, (255, 255, 255), 1)
            for gy in range(0, h // 2, 50):
                cv2.line(crop, (0, gy * scale), (6, gy * scale), (255, 255, 255), 1)
                cv2.putText(crop, str(y0 + gy), (8, gy * scale + 4), cv2.FONT_HERSHEY_PLAIN, 0.8, (255, 255, 255), 1)
            for c in cands + (extra or []):
                x1, y1, x2, y2 = c["box"]
                p1 = (int((x1 - x0) * scale), int((y1 - y0) * scale))
                p2 = (int((x2 - x0) * scale), int((y2 - y0) * scale))
                color = (255, 0, 255) if c.get("added") else COLORS[c["group"]]
                cv2.rectangle(crop, p1, p2, color, 1)
                cv2.putText(crop, str(c["id"]), (p1[0], max(p1[1] - 2, 10)), cv2.FONT_HERSHEY_PLAIN, 0.9, color, 1)
            cv2.imwrite(str(REVIEW / f"{stem}_q{qy}{qx}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])


def load_gt(stem: str) -> list[dict]:
    """bench/gt/<stem>.gt.json: {"keep": [candidate ids], "relabel": {id: group}, "add": [{"group", "box"}], "ignore": [...]}"""
    cands = {c["id"]: c for c in json.loads((GT / f"{stem}.candidates.json").read_text())}
    spec = json.loads((GT / f"{stem}.gt.json").read_text())
    relabel = {int(k): v for k, v in spec.get("relabel", {}).items()}
    gt = [{"group": relabel.get(i, cands[i]["group"]), "box": cands[i]["box"]} for i in spec["keep"]]
    gt += [{"group": a["group"], "box": a["box"], "added": True} for a in spec.get("add", [])]
    # ambiguous regions (unreadable at this resolution): detections centred there are neither TP nor FP
    gt += [{"group": "ignore", "box": b} for b in spec.get("ignore", [])]
    return gt


# ---------------------------------------------------------------- evaluation
def match(dets: list[Detection], gt: list[dict], group: str) -> tuple[int, int, int]:
    truth = [np.array(g["box"]) for g in gt if g["group"] == group]
    preds = sorted((d for d in dets if group_of(d.class_name) == group), key=lambda d: -d.confidence)
    ignore = [g["box"] for g in gt if g["group"] == "ignore"]
    used = [False] * len(truth)
    tp = ignored = 0
    for d in preds:
        b = np.array(d.bbox)
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        best, best_score = -1, 0.0
        for i, g in enumerate(truth):
            if used[i]:
                continue
            ix = max(0, min(b[2], g[2]) - max(b[0], g[0]))
            iy = max(0, min(b[3], g[3]) - max(b[1], g[1]))
            inter = ix * iy
            iou = inter / ((b[2] - b[0]) * (b[3] - b[1]) + (g[2] - g[0]) * (g[3] - g[1]) - inter + 1e-6)
            inside = g[0] <= cx <= g[2] and g[1] <= cy <= g[3]
            score = iou if iou >= 0.3 else (0.29 if inside else 0.0)
            if score > best_score:
                best, best_score = i, score
        if best >= 0:
            used[best] = True
            tp += 1
        elif any(g[0] <= cx <= g[2] and g[1] <= cy <= g[3] for g in ignore):
            ignored += 1
    return tp, len(preds) - tp - ignored, len(truth) - tp  # TP, FP, FN


def speed(cfg: Config, video: str, source_id: str, n: int = 90) -> dict:
    import torch

    det = cfg.detector(video, 0.25)
    cap = cv2.VideoCapture(str(video_path(source_id)))
    batch = []
    while len(batch) < n + 5:
        ok, f = cap.read()
        if not ok:
            break
        batch.append(f)
    for f in batch[:5]:
        det.detect(f)  # warm-up (CUDA kernels, cudnn autotune)
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    times = []
    for f in batch[5:]:
        t = time.perf_counter()
        det.detect(f)
        torch.cuda.synchronize()
        times.append((time.perf_counter() - t) * 1000)
    vram = torch.cuda.max_memory_allocated() / 2**20
    del det
    torch.cuda.empty_cache()
    return {"ms_p50": statistics.median(times), "ms_p95": sorted(times)[int(len(times) * 0.95) - 1], "fps": 1000 / statistics.fmean(times), "vram_mb": vram}


def evaluate(only: list[str] | None, with_sahi: bool) -> None:
    all_frames = frames()
    rows = []
    configs = [c for c in CONFIGS if not only or c.name in only]
    for cfg in configs:
        for video, sid in ((v, s) for s, (v, _) in VIDEOS.items()):
            vf = [(stem, img) for stem, v, img in all_frames if v == video and (GT / f"{stem}.gt.json").exists()]
            if not vf:
                continue
            sp = speed(cfg, video, sid)
            for thr in THRESHOLDS:
                det = cfg.detector(video, thr)
                agg = {g: [0, 0, 0] for g in GROUPS}
                per_frame = {}
                for stem, img in vf:
                    dets = det.detect(img)
                    gt = load_gt(stem)
                    for g in GROUPS:
                        tp, fp, fn = match(dets, gt, g)
                        agg[g] = [a + b for a, b in zip(agg[g], (tp, fp, fn))]
                        if g == "person":
                            per_frame[stem] = (tp + fn, tp, fn, fp)
                rows.append({"config": cfg.name, "video": video, "conf": thr, **sp, "groups": agg, "person_frames": per_frame})
                p = agg["person"]
                v = agg["vehicle"]
                print(f"{cfg.name:18s} {video:6s} conf {thr:.2f} | person TP {p[0]:3d} FP {p[1]:3d} FN {p[2]:3d} R {_r(p):.2f} P {_p(p):.2f} "
                      f"| vehicle R {_r(v):.2f} P {_p(v):.2f} (FP {v[1]}) | {sp['fps']:5.1f} fps p50 {sp['ms_p50']:5.1f} ms VRAM {sp['vram_mb']:5.0f} MB", flush=True)
    if with_sahi:
        rows += sahi_crosscheck(all_frames)
    (ROOT / ("results.json" if not only else "results_extra.json")).write_text(json.dumps(rows, indent=1))


def sahi_crosscheck(all_frames: list) -> list[dict]:
    """The same slicing done by the SAHI library, to validate our implementation."""
    from sahi import AutoDetectionModel
    from sahi.predict import get_sliced_prediction

    rows = []
    model = AutoDetectionModel.from_pretrained(model_type="ultralytics", model_path=str(WEIGHTS / "yolo26s.pt"), confidence_threshold=0.25, device="cuda:0", image_size=640)
    for video, tile in (("plaza", 320), ("aerial", 640)):
        agg = {g: [0, 0, 0] for g in GROUPS}
        times = []
        for stem, v, img in all_frames:
            if v != video or not (GT / f"{stem}.gt.json").exists():
                continue
            t = time.perf_counter()
            res = get_sliced_prediction(img[:, :, ::-1], model, slice_height=tile, slice_width=tile, overlap_height_ratio=0.25, overlap_width_ratio=0.25,
                                        perform_standard_pred=True, postprocess_type="GREEDYNMM", postprocess_match_metric="IOS", postprocess_match_threshold=0.5, verbose=0)
            times.append((time.perf_counter() - t) * 1000)
            dets = [Detection(class_id=o.category.id, class_name=o.category.name, confidence=o.score.value, bbox=tuple(o.bbox.to_xyxy())) for o in res.object_prediction_list]
            gt = load_gt(stem)
            for g in GROUPS:
                agg[g] = [a + b for a, b in zip(agg[g], match(dets, gt, g))]
        p = agg["person"]
        print(f"{'SAHI lib YOLO26s':18s} {video:6s} conf 0.25 | person TP {p[0]:3d} FP {p[1]:3d} FN {p[2]:3d} R {_r(p):.2f} P {_p(p):.2f} | {statistics.median(times):.0f} ms/frame (unbatched)")
        rows.append({"config": "SAHI library YOLO26s", "video": video, "conf": 0.25, "groups": agg, "ms_p50": statistics.median(times)})
    return rows


def _r(a: list[int]) -> float:
    return a[0] / max(a[0] + a[2], 1)


def _p(a: list[int]) -> float:
    return a[0] / max(a[0] + a[1], 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["frames", "candidates", "evaluate", "review"])
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--sahi", action="store_true")
    a = ap.parse_args()
    if a.step == "frames":
        extract()
    elif a.step == "candidates":
        candidates()
    elif a.step == "review":  # re-render with the ground truth (kept = normal colour, added = magenta)
        for stem, _, img in frames():
            if (GT / f"{stem}.gt.json").exists():
                render_review(stem + "_gt", img, [{"id": i, **g} for i, g in enumerate(load_gt(stem)) if g["group"] != "ignore"])
    else:
        evaluate(a.only, a.sahi)
