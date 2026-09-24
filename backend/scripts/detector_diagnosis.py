"""Run detectors DIRECTLY on frames of a video, bypassing the application.

    python scripts/detector_diagnosis.py VIDEO [--times 3 10 30] [--out diagnosis_output]

For each frame and each configuration it saves the raw model output (class id,
class name, confidence, box) to raw.json and an annotated image, so a human can
compare what is visible with what each model reports. Configurations:
  coco_filtered   yolo26s COCO, restricted to the old 9 "CCTV" classes (what the app did)
  coco_all        yolo26s COCO, all 80 classes, no filter
  objects365      yolo26s Objects365 (365 classes), no filter
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OLD_CCTV_FILTER = [0, 1, 2, 3, 5, 7, 24, 26, 28]
CONFIGS = {
    "coco_filtered": ("yolo26s.pt", OLD_CCTV_FILTER),
    "coco_all": ("yolo26s.pt", None),
    "objects365": ("yolo26s-objv1-150.pt", None),
}


def frames_at(video: Path, times: list[float]) -> list[tuple[float, object]]:
    cap = cv2.VideoCapture(str(video))
    out = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if ok:
            out.append((t, frame))
    cap.release()
    return out


def main() -> None:
    from ultralytics import YOLO

    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--times", type=float, nargs="*", default=[3, 10, 20, 30, 45, 60])
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--out", type=Path, default=Path("diagnosis_output"))
    args = ap.parse_args()
    args.out.mkdir(exist_ok=True)

    frames = frames_at(args.video, args.times)
    report: dict = {"video": str(args.video), "confidence_floor": args.conf, "models": {}, "frames": []}
    models = {}
    for name, (weights, _) in CONFIGS.items():
        if weights not in models:
            models[weights] = YOLO(str(Path("models") / weights))
        m = models[weights]
        report["models"][name] = {"weights": weights, "task": m.task, "num_classes": len(m.names), "class_filter": CONFIGS[name][1]}

    for t, frame in frames:
        entry: dict = {"t": t, "results": {}}
        for name, (weights, classes) in CONFIGS.items():
            r = models[weights].predict(frame, conf=args.conf, classes=classes, verbose=False, device="cuda:0")[0]
            dets = [
                {"class_id": int(c), "class": r.names[int(c)], "confidence": round(float(s), 3), "box": [round(float(v), 1) for v in b]}
                for b, s, c in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(), r.boxes.cls.cpu().numpy())
            ]
            entry["results"][name] = sorted(dets, key=lambda d: -d["confidence"])
            cv2.imwrite(str(args.out / f"t{t:05.1f}_{name}.jpg"), r.plot(line_width=2, font_size=14))
        report["frames"].append(entry)
        print(f"t={t:5.1f}s")
        for name, dets in entry["results"].items():
            print(f"   {name:14s} " + (", ".join(f"{d['class']} {d['confidence']:.2f}" for d in dets[:8]) or "(nothing)"))
    (args.out / "raw.json").write_text(json.dumps(report, indent=2))
    print(f"\nraw output: {args.out / 'raw.json'}")


if __name__ == "__main__":
    main()
