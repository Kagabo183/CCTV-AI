# Small / distant object detection benchmark

Measured on our own footage on 24 Sep 2026 with an RTX PRO 4000 Blackwell (24 GB) and FP16. Reproduce with
`backend/scripts/small_object_benchmark.py` (`frames`, `candidates`, `evaluate --sahi`).

## Question

YOLO26s + ByteTrack missed many people that are clearly visible in overhead CCTV. Is the detector the cause, and
which pretrained configuration finds the most genuinely visible small objects without unacceptable false
positives?

The tracker is not the cause. ByteTrack only links boxes the detector produced, so it cannot recover a person the
detector never saw. Every number below is detector-only, before tracking.

## Test set (hand-verified)

| Video | Resolution | Scene | Frames | Visible people | Vehicles | Bicycles |
|---|---|---|---|---|---|---|
| `8491351` "plaza" | 1280×720 | straight-down night view of a plaza and crossing | 4 (t = 2, 6, 14, 22 s) | 47 | 7 | 11 |
| `8319860` "aerial" | 2560×1440 | high oblique daylight view of an intersection | 2 (t = 3, 17 s) | 8 | 42 | 0 |

How the ground truth was made:

1. A deliberately over-permissive union of detectors (YOLO26l @1280, YOLO26s tiled, YOLO26m tiled, conf 0.08)
   proposed candidate boxes.
2. Each candidate was checked by eye on 2–4× zoomed crops. It was kept, relabelled or rejected.
3. Objects that no detector proposed were drawn by hand.
4. Some regions can't be judged at this resolution. These became **ignore** regions, where a detection counts as
   neither a hit nor a false positive. Examples: a densely packed parking lot where cars overlap, people printed
   on billboards, blobs under an awning.

Files: `bench/gt/*.gt.json`.

Scoring: a detection matches a ground-truth object of the same group (person, vehicle or bicycle) when IoU ≥ 0.3,
or when the detection's centre falls inside the ground-truth box. The second rule exists because on 15-pixel
people a few pixels of error swing IoU a lot. Matching is greedy, highest confidence first.

- **Recall** = found / visible.
- **Precision** = correct / all boxes.

## Results: people

Recall (R) and precision (P) at each confidence threshold. Speed is detector-only per frame. p50 latency is at
conf 0.25 on the aerial video.

| Configuration | Plaza R / P @0.50 | @0.35 | @0.25 | @0.15 | Aerial R / P @0.50 | @0.35 | @0.25 | @0.15 | p50 ms | FPS | VRAM |
|---|---|---|---|---|---|---|---|---|---|---|---|
| YOLO26s @640 (previous) | 0.00 / – | 0.00 / – | 0.02 / 0.50 | 0.02 / 0.33 | 0.00 | 0.00 | 0.00 | 0.00 | 8 | 110 | 84 MB |
| YOLO26s @960 | 0.00 | 0.06 / 1.00 | 0.06 / 1.00 | 0.15 / 0.70 | 0.00 | 0.00 | 0.00 | 0.00 | 10 | 96 | 101 MB |
| YOLO26s @1280 | 0.00 | 0.02 / 1.00 | 0.02 / 1.00 | 0.11 / 0.83 | 0.00 | 0.00 | 0.00 | 0.00 | 11 | 84 | 143 MB |
| YOLO26m @1280 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 12 | 78 | 372 MB |
| YOLO26l @1280 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.12 / 1.00 | 16 | 59 | 388 MB |
| **YOLO26s tiled** (chosen) | 0.02 / 1.00 | 0.06 / 0.60 | 0.11 / 0.71 | **0.26 / 0.75** | 0.12 / 1.00 | 0.38 / 1.00 | **0.75 / 1.00** | 0.75 / 1.00 | 60 | 16 | 499 MB |
| YOLO26s tiled, larger tiles | 0.02 / 1.00 | 0.02 / 1.00 | 0.04 / 1.00 | 0.15 / 0.88 | 0.12 / 1.00 | 0.25 / 1.00 | 0.50 / 1.00 | 0.75 / 1.00 | 68 | 14 | 553 MB |
| YOLO26s tiled, smaller tiles | 0.04 / 1.00 | 0.04 / 0.40 | 0.06 / 0.33 | 0.21 / 0.56 | 0.12 / 1.00 | 0.25 / 1.00 | 0.62 / 1.00 | 0.75 / 1.00 | 109 | 9 | 968 MB |
| YOLO26m tiled | 0.02 / 1.00 | 0.02 / 1.00 | 0.04 / 1.00 | 0.06 / 0.60 | 0.00 | 0.00 | 0.00 | 0.25 / 1.00 | 86 | 12 | 1072 MB |
| Objects365 YOLO26s tiled | 0.04 / 1.00 | 0.09 / 0.67 | 0.13 / 0.67 | 0.19 / 0.56 | 0.50 / 1.00 | 0.62 / 1.00 | 0.75 / 0.86 | 0.75 / 0.86 | 60 | 16 | 490 MB |
| SAHI library, YOLO26s (cross-check) | – | – | 0.11 / 0.62 | – | – | – | 0.75 / 1.00 | – | 254 | 4 | – |

Configuration details:

- **Tiled** means overlapping tiles plus one full-frame pass at 1280, merged per class by greedy non-maximum merging
  (intersection over the smaller box, 0.5). Tiles overlap by 25% and are run at 640. Tile sizes:

  | Variant | Plaza tile | Aerial tile |
  |---|---|---|
  | Tiled | 320 px (upscaled 2×) | 640 px |
  | Larger tiles | 480 px | 960 px (run at 960) |
  | Smaller tiles | 240 px | 480 px |

- **SAHI library cross-check.** The SAHI package, run on the same slicing, gives the same recall as our
  implementation on the aerial video, which confirms our tiling is correct. It is about 4× slower because it runs
  tiles one at a time, while we batch them.

## Results: vehicles (aerial video, 42 vehicles)

| Configuration | R / P @0.50 | @0.35 | @0.25 | @0.15 |
|---|---|---|---|---|
| YOLO26s @640 | 0.12 / 1.00 | 0.17 / 1.00 | 0.21 / 1.00 | 0.33 / 0.88 |
| YOLO26s @1280 | 0.26 / 0.92 | 0.31 / 0.93 | 0.36 / 0.94 | 0.43 / 0.90 |
| YOLO26m @1280 | 0.33 / 0.93 | 0.45 / 0.95 | 0.48 / 0.91 | 0.52 / 0.88 |
| YOLO26l @1280 | 0.36 / 0.94 | 0.48 / 0.95 | 0.50 / 0.95 | 0.55 / 0.88 |
| **YOLO26s tiled** | 0.33 / 1.00 | 0.48 / 0.87 | **0.62 / 0.90** | 0.67 / 0.80 |
| YOLO26m tiled | 0.45 / 0.90 | 0.52 / 0.88 | 0.52 / 0.85 | 0.60 / 0.86 |

The plaza has only 7 vehicles, all at the frame edges. Tiling there mostly added false positives, such as tables
and planters read as cars: 4 at conf 0.25 and 11 at conf 0.15.

## Per-frame detail (people, chosen configuration, conf 0.25)

| Frame | Visible | Detected (TP) | Missed | False positives |
|---|---|---|---|---|
| plaza t=2 s | 12 | 3 | 9 | 0 |
| plaza t=6 s | 12 | 2 | 10 | 0 |
| plaza t=14 s | 9 | 0 | 9 | 0 |
| plaza t=22 s | 14 | 0 | 14 | 2 |
| aerial t=17 s | 8 | 6 | 2 | 0 |

The full per-frame numbers for every configuration are in `bench/results.json`, under `person_frames`.

## Findings

1. **Resolution alone does not help.** On the 720p plaza, 1280 is already native resolution, and larger models
   (m, l) found *fewer* overhead people than s. On the 1440p aerial video, no full-frame configuration found a
   single pedestrian at a usable threshold.
2. **Tiling is the only lever that works for small objects.** On the aerial video, people went from 0/8 to 6/8
   with no false positives, and vehicles from 36% to 62% recall at 90% precision (conf 0.25). This matches
   Ultralytics' guidance on SAHI for small objects in high-resolution frames.
3. **Tile size matters.** Tiles of about 45% of the frame's short side worked best. Bigger tiles shrink people
   again; smaller tiles cut through them and add false positives.

   The rounding matters too. A 648 px tile halved aerial person recall compared with a 640 px tile, so the automatic
   tile size is rounded to a multiple of 32. This also means the aerial person numbers rest on only 8 people and
   should be read as indicative.
4. **Straight-down night views defeat every pretrained model.** The best plaza result was recall 0.26 at precision
   0.75, and only at conf 0.15. From directly above, a person is a head and shoulders, a view that COCO and
   Objects365 barely contain. No threshold, resolution or model size fixes that. This is a **training-data**
   problem, so we move to fine-tuning, as planned.

## Chosen configuration (now the default)

```
VISION_DETECTOR=yolo            # yolo26s.pt
VISION_IMAGE_SIZE=1280          # full-frame pass
VISION_TILING=on
VISION_TILE_SIZE=0              # auto: 45% of the short side, rounded to 32 (320 on 720p, 640 on 1440p)
VISION_TILE_OVERLAP=0.25
VISION_TILE_IMAGE_SIZE=640
VISION_TILE_FULL_FRAME=true     # keeps large, near objects
VISION_CONFIDENCE=0.25          # equals ByteTrack's new-track threshold
VISION_TILE_CLASSES=person,bicycle,car,motorcycle,bus,truck  # classes taken from tiles
VISION_TRACKER=bytetrack        # unchanged; runs after detection
```

- **Why conf 0.25 and not 0.15.** At 0.15, plaza person recall rises from 0.11 to 0.26, but vehicle false positives
  triple and ByteTrack would not start tracks from those boxes anyway (its `new_track_thresh` is 0.25).
- **Cost.** About 60 ms per frame against 9 ms. At the 10 frames per second we sample, an 89-second video takes
  about 55 s of GPU time instead of about 8 s. For a live camera it is about 16 FPS per stream on this GPU.
- **Why tiles only keep people and vehicles.** In the first full runs with tiling on, upscaled tiles made the
  model report confident cows, cakes and aeroplanes in the night plaza, and boats and laptops on the street.
  Tiling was only validated for people and vehicles, so other classes now come from the full-frame pass alone.
  Measured with this filter on (conf 0.25): plaza people 0.11 recall / 0.71 precision (unchanged), aerial people
  0.75 / 1.00 (unchanged), aerial vehicles 0.60 / 0.93 (was 0.62 / 0.90).
- **Everything is configurable.** Detector, weights, full-frame size, tile size, overlap, tile inference size,
  full-frame pass, confidence and tracker are all settings. For a site that only needs large, near objects, set
  `VISION_TILING=off` to get the old speed back.

## Next step: fine-tuning (prepared, not run)

`backend/scripts/prepare_finetune_dataset.py` builds a YOLO-format dataset from our footage at
`datasets/cctv_v1/`. The folder is git-ignored because it contains video frames.

- 1 frame per second, with the last 20% of each video held out for validation.
- 54 images: 6 carry our hand-verified labels and 48 carry pre-labels from the chosen detector at conf 0.15.
- COCO class ids are kept (0 person, 1 bicycle, 2 car, 3 motorcycle, 5 bus, 7 truck), so a fine-tuned model is a
  drop-in replacement for `yolo26s.pt`.

Before any training:

1. Import `datasets/cctv_v1` into CVAT or Label Studio (YOLO 1.1 format), then correct every pre-label and draw the
   missed people. Ignore regions in the verified frames are unlabelled and must be labelled too; otherwise the model
   learns them as background.
2. Add more overhead and night footage from our own cameras. Two videos are far too few; aim for 500 or more
   labelled images across several sites and lighting conditions.
3. Only then fine-tune `yolo26s.pt`. Evaluate on this benchmark's frames plus a held-out site, and keep tiling on.
