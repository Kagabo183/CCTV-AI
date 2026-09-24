"""Which object classes do the detectors actually find? Measured on labelled photos.

    python scripts/class_coverage.py [--dataset coco128]

Uses Ultralytics' coco128 (128 real COCO photos with ground-truth labels):
  * COCO YOLO26s: per-class image-level recall (was the labelled class found in that image?)
  * Objects365 YOLO26s: number of distinct classes it reports on the same photos
    (different vocabulary: no per-class recall without mapping, reported as diversity)
Writes coverage_output/coverage.json.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from ultralytics import YOLO
    from ultralytics.data.utils import check_det_dataset

    data = check_det_dataset("coco128.yaml")  # downloads ~7 MB on first use
    images = sorted(Path(data["train"]).glob("*.jpg"))
    names = data["names"]
    coco = YOLO("models/yolo26s.pt")
    o365 = YOLO("models/yolo26s-objv1-150.pt")

    labelled: dict[str, int] = Counter()
    found: dict[str, int] = Counter()
    coco_conf: dict[str, list[float]] = defaultdict(list)
    o365_classes: Counter = Counter()
    coco_classes: Counter = Counter()
    for img in images:
        label_file = Path(str(img).replace("images", "labels")).with_suffix(".txt")
        truth = {names[int(line.split()[0])] for line in label_file.read_text().splitlines() if line.strip()} if label_file.exists() else set()
        r = coco.predict(str(img), conf=0.25, verbose=False, device="cuda:0")[0]
        seen = {}
        for c, s in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist()):
            n = r.names[int(c)]
            seen[n] = max(seen.get(n, 0.0), s)
        coco_classes.update(seen)
        for cls in truth:
            labelled[cls] += 1
            if cls in seen:
                found[cls] += 1
                coco_conf[cls].append(seen[cls])
        r2 = o365.predict(str(img), conf=0.25, verbose=False, device="cuda:0")[0]
        o365_classes.update({r2.names[int(c)] for c in r2.boxes.cls.tolist()})

    per_class = {
        cls: {"images_with_class": labelled[cls], "found": found[cls], "recall": round(found[cls] / labelled[cls], 2),
              "mean_conf_when_found": round(sum(coco_conf[cls]) / len(coco_conf[cls]), 2) if coco_conf[cls] else None}
        for cls in sorted(labelled, key=lambda c: -labelled[c])
    }
    out = {
        "images": len(images),
        "coco_yolo26s": {"model_classes": len(coco.names), "distinct_classes_detected": len(coco_classes), "per_class_recall": per_class},
        "objects365_yolo26s": {"model_classes": len(o365.names), "distinct_classes_detected": len(o365_classes), "top_classes": o365_classes.most_common(40)},
    }
    Path("coverage_output").mkdir(exist_ok=True)
    Path("coverage_output/coverage.json").write_text(json.dumps(out, indent=2))
    print(f"{len(images)} labelled photos, {len(labelled)} ground-truth classes present")
    print(f"COCO YOLO26s: {len(coco.names)} classes in model, {len(coco_classes)} distinct classes detected")
    print(f"Objects365 YOLO26s: {len(o365.names)} classes in model, {len(o365_classes)} distinct classes detected")
    print("\nCOCO per-class image recall (class: found/labelled, mean conf):")
    for cls, v in per_class.items():
        print(f"  {cls:15s} {v['found']:2d}/{v['images_with_class']:<2d} recall {v['recall']:.2f}  conf {v['mean_conf_when_found']}")
    print("\nObjects365 most frequent classes:", ", ".join(f"{c} {n}" for c, n in o365_classes.most_common(25)))


if __name__ == "__main__":
    main()
