"""Build a YOLO fine-tuning dataset from our own CCTV footage (prepared, NOT trained).

    python scripts/prepare_finetune_dataset.py --source <video source id> [--source ...] [--every 1.0] [--out ../datasets/cctv_v1]

Why: docs/SMALL_OBJECT_BENCHMARK.md shows every pretrained COCO / Objects365 model
misses most people seen from straight above at night (best recall 0.26). That
viewpoint is rare in their training data, so the fix is labelled examples of it.

What it writes (Ultralytics / YOLO 1.1 format, loadable by CVAT and Label Studio):
    images/{train,val}/<video>_<t>.jpg
    labels/{train,val}/<video>_<t>.txt      pre-labels, class x_c y_c w h (normalised)
    dataset.yaml                            class names, paths
    manifest.csv                            every image: split, verified (yes/no), source, second
Pre-labels come from the platform's own detector at a low threshold (0.15) so the
annotator mostly deletes and nudges rather than drawing from scratch. Frames we
already verified by hand (bench/gt) are copied as verified labels.

Split: the last 20% of each video is validation, so near-identical neighbouring
frames never end up on both sides.

Nothing here trains a model. Pre-labels MUST be reviewed before training.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import cv2

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
sys.stdout.reconfigure(encoding="utf-8")

from small_object_benchmark import GT, VIDEOS, WEIGHTS, group_of, load_gt, video_path  # noqa: E402

from app.vision.detectors import TilingConfig, YOLODetector  # noqa: E402

# Keep COCO's ids for these classes so a fine-tuned model stays drop-in compatible with yolo26s.pt heads.
CLASSES = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
GT_GROUP_TO_CLASS = {"person": 0, "bicycle": 1, "vehicle": 2}


def yolo_line(cls: int, box: list[float] | tuple[float, ...], w: int, h: int) -> str:
    x1, y1, x2, y2 = (max(0.0, min(v, lim)) for v, lim in zip(box, (w, h, w, h)))
    return f"{cls} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", required=True, help="video source id (repeatable)")
    ap.add_argument("--every", type=float, default=1.0, help="seconds between sampled frames")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "datasets" / "cctv_v1"))
    ap.add_argument("--prelabel-confidence", type=float, default=0.15)
    a = ap.parse_args()

    out = Path(a.out)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    det = YOLODetector("yolo", "yolo26s.pt", weights_dir=WEIGHTS, confidence=a.prelabel_confidence, image_size=1280,
                       tiling=TilingConfig(), classes=list(CLASSES.values()))
    rows = []
    for sid in a.source:
        name = VIDEOS.get(sid, (sid[:8], []))[0]
        cap = cv2.VideoCapture(str(video_path(sid)))
        fps, n = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = n / fps
        t = 0.0
        while t < duration:
            cap.set(cv2.CAP_PROP_POS_FRAMES, round(t * fps))
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            split = "val" if t >= duration * 0.8 else "train"
            stem = f"{name}_{t:06.1f}".replace(".", "_")
            gt_stem = f"{name}_{int(t):02d}"
            verified = t == int(t) and (GT / f"{gt_stem}.gt.json").exists()
            if verified:  # hand-checked boxes (ignore regions are dropped: unlabelled, not background)
                lines = [yolo_line(GT_GROUP_TO_CLASS[g["group"]], g["box"], w, h) for g in load_gt(gt_stem) if g["group"] in GT_GROUP_TO_CLASS]
            else:
                lines = [yolo_line(d.class_id, d.bbox, w, h) for d in det.detect(frame) if group_of(d.class_name)]
            cv2.imwrite(str(out / "images" / split / f"{stem}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            (out / "labels" / split / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
            rows.append({"image": f"images/{split}/{stem}.jpg", "split": split, "verified": "yes" if verified else "no", "source": sid, "second": round(t, 1), "boxes": len(lines)})
            t += a.every
        print(f"{name}: {sum(r['source'] == sid for r in rows)} frames")

    names = "\n".join(f"  {i}: {n}" for i, n in CLASSES.items())
    (out / "dataset.yaml").write_text(f"path: {out.resolve().as_posix()}\ntrain: images/train\nval: images/val\nnames:\n{names}\n")
    with (out / "manifest.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    todo = sum(r["verified"] == "no" for r in rows)
    print(json.dumps({"images": len(rows), "verified": len(rows) - todo, "to_review": todo, "out": str(out)}))


if __name__ == "__main__":
    main()
