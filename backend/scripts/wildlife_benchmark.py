"""Wildlife benchmark on the platform's documentary, against hand labels (bench/wildlife/gt.json).

    python scripts/wildlife_benchmark.py detect     # animal detectors: count accuracy, speed, VRAM
    python scripts/wildlife_benchmark.py species    # species labels per detected animal, per classifier
    python scripts/wildlife_benchmark.py table      # per-frame table for the report

Nothing here is estimated: every number comes from running the models on the labelled frames.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2] / "bench" / "wildlife"
MODELS = Path(__file__).resolve().parents[1] / "models"
GT = json.loads((ROOT / "gt.json").read_text())["frames"]
COCO_ANIMALS = {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}

# name -> (weights, loader, image size, class filter)
DETECTORS = {
    "YOLO26s tiled (current)": ("yolo26s.pt", "platform", 1280, COCO_ANIMALS),
    "MDV6-yolov9-c": ("wildlife/MDV6-yolov9-c.pt", "yolo", 640, {"animal"}),
    "MDV6-yolov9-e": ("wildlife/MDV6-yolov9-e-1280.pt", "yolo", 1280, {"animal"}),
    "MDV6-yolov10-c": ("wildlife/MDV6-yolov10-c.pt", "yolo", 640, {"animal"}),
    "MDV6-yolov10-e": ("wildlife/MDV6-yolov10-e-1280.pt", "yolo", 1280, {"animal"}),
    "MDV6-rtdetr-c": ("wildlife/MDV6-rtdetr-c.pt", "rtdetr", 640, {"animal"}),
    "African Wildlife YOLO26s": ("wildlife/african_wildlife/yolo26s/weights/best.pt", "yolo", 640, {"buffalo", "elephant", "rhino", "zebra"}),
}

SYNONYMS = {
    "elephant": ("elephant", "loxodonta"), "zebra": ("zebra",), "giraffe": ("giraffe",), "rhinoceros": ("rhino",),
    "hippopotamus": ("hippo",), "buffalo": ("buffalo", "syncerus"), "wildebeest": ("wildebeest", "connochaetes"),
    "lion": ("lion",), "cheetah": ("cheetah",), "leopard": ("leopard",),
}


def frame(t: int) -> np.ndarray:
    return cv2.imread(str(ROOT / "frames" / f"t{t:04d}.jpg"))


def to_species(label: str) -> str | None:
    label = label.lower()
    for sp, keys in SYNONYMS.items():
        if any(re.search(rf"\b{k}", label) for k in keys):
            return sp
    return None


def load(name: str):  # type: ignore[no-untyped-def]
    weights, kind, size, _ = DETECTORS[name]
    path = MODELS / weights
    if kind == "platform":
        from app.vision.detectors import TilingConfig, YOLODetector

        det = YOLODetector("yolo", "yolo26s.pt", weights_dir=MODELS, confidence=0.25, image_size=1280, tiling=TilingConfig())
        return lambda img, conf: [(d.class_name, d.confidence, d.bbox) for d in det.detect(img) if d.confidence >= conf]
    from ultralytics import RTDETR, YOLO

    model = (RTDETR if kind == "rtdetr" else YOLO)(str(path))

    def run(img, conf):  # type: ignore[no-untyped-def]
        r = model.predict(img, imgsz=size, conf=conf, device=0, half=True, verbose=False)[0]
        names = r.names
        return [(names[int(c)], float(s), tuple(map(float, b))) for b, s, c in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(), r.boxes.cls.cpu().numpy())]

    return run


def animals(name: str, dets: list) -> list:
    return [d for d in dets if d[0] in DETECTORS[name][3]]


def detect_step() -> None:
    import torch

    rows = {}
    for name in DETECTORS:
        if not (MODELS / DETECTORS[name][0]).exists():
            print(f"{name}: weights missing, skipped")
            continue
        run = load(name)
        for t in (112, 530):  # warm-up
            run(frame(t), 0.25)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        times, per = [], {}
        for g in GT:
            img = frame(g["t"])
            t0 = time.perf_counter()
            dets = run(img, 0.1)
            torch.cuda.synchronize()
            times.append((time.perf_counter() - t0) * 1000)
            per[g["t"]] = dets
        vram = torch.cuda.max_memory_allocated() / 2**20
        for conf in (0.25, 0.5):
            counted = [g for g in GT if g["count"]]
            found = missed = extra = 0
            frames_with_animal = 0
            for g in GT:
                a = [d for d in animals(name, per[g["t"]]) if d[1] >= conf]
                frames_with_animal += bool(a)
                if g["count"]:
                    n = len(a)
                    found += min(n, g["count"])
                    missed += max(0, g["count"] - n)
                    extra += max(0, n - g["count"])
            total = sum(g["count"] for g in counted)
            rows[(name, conf)] = {"recall": found / total, "missed": missed, "extra": extra, "frames_with_animal": f"{frames_with_animal}/{len(GT)}",
                                  "ms_p50": statistics.median(times), "vram_mb": vram}
            r = rows[(name, conf)]
            print(f"{name:26s} conf {conf:.2f} | counted animals found {found}/{total} (recall {r['recall']:.2f}), missed {missed}, extra boxes {extra} "
                  f"| frames with an animal {r['frames_with_animal']} | {r['ms_p50']:.1f} ms/frame | VRAM {vram:.0f} MB", flush=True)
        (ROOT / f"det_{re.sub(r'[^a-z0-9]+', '_', name.lower())}.json").write_text(json.dumps({str(k): v for k, v in per.items()}))
        del run
        torch.cuda.empty_cache()


def species_step(detector: str) -> None:
    from app.wildlife.species import SerengetiClassifier, SpeciesNetClassifier

    dets = json.loads((ROOT / f"det_{re.sub(r'[^a-z0-9]+', '_', detector.lower())}.json").read_text())
    yolo = json.loads((ROOT / "det_yolo26s_tiled_current_.json").read_text())
    african = ROOT / "det_african_wildlife_yolo26s.json"
    african = json.loads(african.read_text()) if african.exists() else {}
    classifiers = {"SpeciesNet (geofence RWA)": SpeciesNetClassifier(country="RWA"), "SpeciesNet (no geofence)": SpeciesNetClassifier(country=None),
                   "Snapshot Serengeti": SerengetiClassifier()}
    table, score = [], {k: Counter() for k in ["YOLO26s (COCO)", "African Wildlife YOLO26s", *classifiers]}

    def judge(label: str, rank: str, truth: list[str]) -> str:
        sp = to_species(label)
        if sp in truth:
            return "correct"
        if sp is None and (rank not in ("species",) or label in ("other", "animal", "mammal")):
            return "uncertain"
        return "wrong"

    for g in GT:
        t, truth = g["t"], g["species"]
        img = frame(t)
        boxes = [d for d in dets[str(t)] if d[0] == "animal" and d[1] >= 0.25]
        boxes.sort(key=lambda d: -((d[2][2] - d[2][0]) * (d[2][3] - d[2][1])))
        boxes = boxes[:8]  # the 8 largest animals in the frame
        row = {"t": t, "truth": "/".join(truth), "animals_detected": len(boxes)}
        # current platform: COCO labels on its own boxes
        ya = [d for d in yolo[str(t)] if d[0] in COCO_ANIMALS and d[1] >= 0.25]
        for d in ya:
            score["YOLO26s (COCO)"][judge(d[0], "species", truth)] += 1
        row["yolo"] = ", ".join(f"{k} ×{n}" for k, n in Counter(d[0] for d in ya).most_common(3)) or "no animal"
        aw = [d for d in african.get(str(t), []) if d[1] >= 0.25]
        for d in aw:
            score["African Wildlife YOLO26s"][judge(d[0], "species", truth)] += 1
        row["african"] = ", ".join(f"{k} ×{n}" for k, n in Counter(d[0] for d in aw).most_common(3)) or "no animal"
        for cname, clf in classifiers.items():
            results = clf.classify(img, [d[2] for d in boxes], [d[1] for d in boxes])
            labels = []
            for r in results:
                verdict = judge(r.label, r.rank, truth)
                score[cname][verdict] += 1
                labels.append(f"{r.label} {r.score:.2f}")
            names = [lab.rsplit(" ", 1)[0] for lab in labels]
            row[cname] = "; ".join(f"{k} ×{n}" for k, n in Counter(names).most_common(3)) or "—"
            row[cname + " scores"] = [round(r.score, 2) for r in results]
        table.append(row)
        print(t, row["truth"], "| YOLO:", row["yolo"], "| SpeciesNet RWA:", row["SpeciesNet (geofence RWA)"], "| Serengeti:", row["Snapshot Serengeti"], flush=True)
    print("\nPer detected animal (boxes of", detector, "for classifiers; each model's own boxes for YOLO models):")
    for k, c in score.items():
        n = sum(c.values()) or 1
        print(f"  {k:28s} correct {c['correct']:3d} ({c['correct']/n:.0%}) | uncertain {c['uncertain']:3d} ({c['uncertain']/n:.0%}) | wrong {c['wrong']:3d} ({c['wrong']/n:.0%}) | n={n}")
    (ROOT / "species_results.json").write_text(json.dumps({"detector": detector, "table": table, "score": {k: dict(v) for k, v in score.items()}}, indent=1))




RWANDA_EXTRA = ["equus quagga", "giraffa camelopardalis", "ceratotherium simum", "diceros bicornis"]


def final_step(detector: str) -> None:
    """The platform's identifier (SpeciesNet + no-forcing rules [+ specialist]) on the detector's boxes."""
    from ultralytics import YOLO

    from app.wildlife.identify import WildlifeIdentifier
    from app.wildlife.species import SpeciesNetClassifier

    dets = json.loads((ROOT / f"det_{re.sub(r'[^a-z0-9]+', '_', detector.lower())}.json").read_text())
    specialist = YOLO(str(MODELS / DETECTORS["African Wildlife YOLO26s"][0]))
    configs = {
        "SpeciesNet, no geofence": WildlifeIdentifier(SpeciesNetClassifier(country=None)),
        "SpeciesNet, RWA + allow-list": WildlifeIdentifier(SpeciesNetClassifier(country="RWA", extra_allow=RWANDA_EXTRA)),
        "SpeciesNet, RWA + allow-list + specialist": WildlifeIdentifier(SpeciesNetClassifier(country="RWA", extra_allow=RWANDA_EXTRA), specialist),
    }
    scores = {k: Counter() for k in configs}
    rows = []
    for g in GT:
        t, truth = g["t"], g["species"]
        img = frame(t)
        boxes = sorted((d for d in dets[str(t)] if d[0] == "animal" and d[1] >= 0.25), key=lambda d: -((d[2][2] - d[2][0]) * (d[2][3] - d[2][1])))[:8]
        row = {"t": t, "truth": "/".join(truth)}
        for name, ident in configs.items():
            labels = ident.identify(img, [d[2] for d in boxes], [d[1] for d in boxes])
            for lab in labels:
                if lab.certain:
                    scores[name]["correct" if to_species(lab.species or "") in truth else "wrong"] += 1
                else:
                    scores[name]["uncertain, guess right" if to_species(lab.candidate or "") in truth else "uncertain"] += 1
            row[name] = "; ".join(f"{k} ×{n}" for k, n in Counter(l.display for l in labels).most_common(3)) or "no animal detected"
        rows.append(row)
        print(t, row["truth"], "|", row["SpeciesNet, RWA + allow-list + specialist"], flush=True)
    print()
    for k, c in scores.items():
        n = sum(c.values()) or 1
        print(f"  {k:44s} correct {c['correct']/n:4.0%} | wrong {c['wrong']/n:4.0%} | uncertain (guess right) {c['uncertain, guess right']/n:4.0%} | uncertain {c['uncertain']/n:4.0%} | n={n}")
    (ROOT / "final_results.json").write_text(json.dumps({"detector": detector, "rows": rows, "scores": {k: dict(v) for k, v in scores.items()}}, indent=1))


def table_step() -> None:
    """Per-frame markdown table (bench/wildlife/per_frame_table.md) from the saved results."""
    sp = json.loads((ROOT / "species_results.json").read_text())
    fin = json.loads((ROOT / "final_results.json").read_text())
    det = json.loads((ROOT / "det_mdv6_yolov10_e.json").read_text())
    gt = {g["t"]: g for g in GT}
    final_rows = {r["t"]: r for r in fin["rows"]}

    def verdict(cell: str, truth: list[str]) -> str:
        first = cell.split(";")[0].split(" ×")[0].strip().lower()
        if not first or first.startswith("no animal"):
            return "–"
        hit = to_species(first) in truth
        if first.startswith(("possible", "animal")):
            return "uncertain (guess right)" if hit else "uncertain"
        return "yes" if hit else "no"

    rows = ["| Time | Actual animal | Current YOLO26s (COCO) | MegaDetector V6 animals found | Platform label (SpeciesNet + rules) | Correct? |", "|---|---|---|---|---|---|"]
    for r in sp["table"]:
        t = r["t"]
        truth = gt[t]["species"]
        found = sum(1 for d in det[str(t)] if d[0] == "animal" and d[1] >= 0.25)
        final = final_rows[t]["SpeciesNet, RWA + allow-list + specialist"]
        count = f"{found}" + (f" (actual {gt[t]['count']})" if gt[t]["count"] else "")
        rows.append(f"| {t // 60}:{t % 60:02d} | {' / '.join(truth)} | {r['yolo']} | {count} | {final} | {verdict(final, truth)} |")
    (ROOT / "per_frame_table.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"{len(rows) - 2} rows -> {ROOT / 'per_frame_table.md'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["detect", "species", "final", "table"])
    ap.add_argument("--detector", default="MDV6-yolov9-c")
    a = ap.parse_args()
    {"detect": detect_step, "species": lambda: species_step(a.detector), "final": lambda: final_step(a.detector), "table": table_step}[a.step]()
