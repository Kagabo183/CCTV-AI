"""Benchmark detector x tracker combinations on the same test videos.

    python scripts/benchmark_vision.py [--videos sample_videos/*.mp4] [--sample-fps 10]

Writes benchmark_output/results.json, annotated videos, and sample frames
(for visual inspection). No ground-truth labels exist for these clips, so
tracking "quality" is measured through proxies (fragmentation, track lifetime,
count stability), plus manual inspection of the annotated output.
"""

from __future__ import annotations

import argparse
import gc
import glob
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.vision.detectors import build_detector  # noqa: E402
from app.vision.events import EventRules, Line, SceneConfig, Zone  # noqa: E402
from app.vision.pipeline import VisionPipeline  # noqa: E402
from app.vision.trackers import build_tracker  # noqa: E402

COMBOS = [("yolo", "bytetrack"), ("yolo", "botsort"), ("rtdetr", "bytetrack"), ("rtdetr", "botsort")]

# A generic scene so the event engine has something to evaluate: a tripwire
# across the lower-middle of the frame and a zone covering the bottom third.
DEMO_SCENE = SceneConfig(
    zones=[Zone("lower_area", [(0.0, 0.66), (1.0, 0.66), (1.0, 1.0), (0.0, 1.0)])],
    lines=[Line("midline", (0.0, 0.55), (1.0, 0.55))],
    rules=EventRules(dwell_seconds=10, loiter_seconds=20, crowd_threshold=3),
)


def count_stability(snapshots: list, cls: str = "person") -> float:
    """Mean absolute change in per-second object count: lower = steadier."""
    counts = [s.counts.get(cls, 0) for s in snapshots]
    if len(counts) < 2:
        return 0.0
    return round(sum(abs(a - b) for a, b in zip(counts, counts[1:])) / (len(counts) - 1), 3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--videos", nargs="*", default=sorted(glob.glob("sample_videos/*.mp4")))
    parser.add_argument("--sample-fps", type=float, default=10.0)
    parser.add_argument("--out", default="benchmark_output")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    weights_dir = Path("models")

    results = []
    detectors = {}
    for det_name, trk_name in COMBOS:
        if det_name not in detectors:
            detectors.clear()
            gc.collect()
            detectors[det_name] = build_detector(det_name, weights_dir=weights_dir)
            detectors[det_name].warmup()
        detector = detectors[det_name]
        for video in args.videos:
            name = Path(video).stem
            tracker = build_tracker(trk_name, processing_fps=args.sample_fps)
            pipeline = VisionPipeline(detector, tracker, DEMO_SCENE, sample_fps=args.sample_fps)
            annotated = out / f"{name}__{det_name}_{trk_name}.mp4"
            result = pipeline.run(video, annotate_to=annotated)
            stats = result.stats
            stats.update(video=name, combo=f"{det_name}+{trk_name}", person_count_jitter=count_stability(result.snapshots))
            results.append(stats)
            print(f"{name:32s} {det_name:6s}+{trk_name:9s} {stats['processing_fps']:6.1f} fps  det p50 {stats['detect_ms_p50']:5.1f}ms  "
                  f"gpu {stats['gpu_peak_mb']}MB  tracks {stats['tracks']['total']:3d} (short {stats['tracks']['short_lived']:2d})  events {sum(stats['events'].values())}")
            if trk_name == "bytetrack":
                save_sample_frames(annotated, result, out / "frames" / f"{name}__{det_name}")
    (out / "results.json").write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out / 'results.json'}")


def save_sample_frames(annotated: Path, result, prefix: Path) -> None:
    """Save the annotated frames with the most tracked objects, for manual inspection."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    busiest = sorted(result.snapshots, key=lambda s: -len(s.track_ids))[:2]
    cap = cv2.VideoCapture(str(annotated))
    fps = result.stats["sample_fps"]
    for snap in busiest:
        cap.set(cv2.CAP_PROP_POS_FRAMES, round(snap.timestamp * fps))
        ok, frame = cap.read()
        if ok:
            cv2.imwrite(f"{prefix}__t{snap.timestamp:05.1f}.jpg", frame)
    cap.release()


if __name__ == "__main__":
    main()
