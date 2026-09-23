# Local vision benchmark: detector × tracker

**Recommendation: `VISION_DETECTOR=yolo` (yolo26s.pt) + `VISION_TRACKER=bytetrack`.** It is 2× faster than RT-DETR, uses less GPU memory, and produced clean, stable tracks on every clip. RT-DETR drew duplicate boxes on vehicles and made more false positives. BoT-SORT gave the same tracks as ByteTrack but ran slower, because its camera-motion compensation doesn't help fixed CCTV cameras.

These are **pretrained COCO models with no fine-tuning**. Re-run the benchmark on real footage from your cameras before tuning anything: `python scripts/benchmark_vision.py --videos path/to/*.mp4`.

## Setup

| | |
|---|---|
| GPU | NVIDIA RTX PRO 4000 Blackwell, 24 GB (compute 12.0), driver CUDA 13.2 |
| Software | PyTorch 2.14.0+cu130, Ultralytics 8.4.160, OpenCV 5.0, FP16 inference, 640 px input |
| Models | `yolo26s.pt` (19.5 MB), `rtdetr-l.pt` (COCO-pretrained) |
| Classes | person, bicycle, car, motorcycle, bus, truck, backpack, handbag, suitcase |
| Sampling | 10 frames/s (CCTV rarely needs more), confidence ≥ 0.25 |
| Test videos | Intel IoT DevKit sample clips (public): parking lot (person/bicycle/car, 54 s, 768×432), cars (30 s), people in a hallway (50 s), warehouse workers (76 s, **1080p 60 fps**) |

There are no ground-truth labels for these clips. Detection quality was judged by **visual inspection of annotated frames** plus consistency statistics. Tracking quality was judged through proxies: track count against the visible number of objects, short-lived fragments (< 1 s), and jitter in the per-second person count.

## Results (mean over the 4 clips; 1080p clip listed separately)

| Combination | Throughput 768×432 | Throughput 1080p | Detect latency p50 / p95 | GPU memory (process) | Tracks / short fragments (car clip) |
|---|---|---|---|---|---|
| **YOLO26s + ByteTrack** | **~90 fps** | **42 fps** | **8.6 / 17.5 ms** | **~330 MB** | **4 / 0** |
| YOLO26s + BoT-SORT | ~73 fps | 30 fps | 9.3 / 20 ms | ~330 MB | 4 / 0 |
| RT-DETR-L + ByteTrack | ~48 fps | 31 fps | 17 / 32 ms | ~460 MB | 13 / 6 |
| RT-DETR-L + BoT-SORT | ~44 fps | 24 fps | 17.4 / 32 ms | ~460 MB | 12 / 3 |

CPU usage was 4–8 % of 24 cores in every configuration, because decoding and inference run on the GPU. Throughput counts processed frames per second, including decoding, detection, tracking and the event engine. At 10 fps sampling, **one GPU running YOLO26s + ByteTrack can keep up with roughly 9 SD streams or 4 × 1080p streams in real time**. That's before any batching, which would raise it.

### Detection quality: what the annotated frames showed
- **YOLO26s:** tight boxes on people (0.90–0.94 in the hallway and warehouse), the car (0.93), and cyclists. There were almost no stray classes: one 0.29 "truck" across all clips. Detections were stable across thresholds 0.25–0.5.
- **RT-DETR-L:** drew **two overlapping boxes on the same car**. It produced 417 car detections versus YOLO's 162 on identical frames, and 306 were still there at confidence 0.5. It also added low-confidence false positives on empty scenes ("truck", "bus", "suitcase", "handbag" at 0.25–0.37). Each duplicate becomes an extra track and fake `object_appeared` / `object_disappeared` events. It did find more **bicycles** (117 vs 67 detections), so it may be worth re-testing on scenes where small, partly occluded objects matter.

### Tracking: ByteTrack vs BoT-SORT
- Identities held through crossings in the hallway clip: three people walking together kept IDs #6, #7 and #8. Two warehouse workers kept IDs #5 and #6 for about 10 s each (mean track life 9.8 s).
- Both trackers gave **the same track counts** on 3 of 4 clips with YOLO. BoT-SORT had one fewer fragment on the parking-lot clip (9 vs 10 tracks).
- BoT-SORT's global motion compensation (sparse optical flow) costs 15–30 % throughput. Its benefit is for moving or PTZ cameras. ReID is off by default. Enabling it would help with long occlusions but costs another model pass per frame.

**Choose BoT-SORT with ReID when** a camera pans, or when people are often hidden behind pillars or vehicles for several seconds. Re-test on that camera's footage first.

## Limitations
- COCO has no classes for weapons, fire or smoke, and pretrained models aren't tuned for CCTV angles, night or infrared. Those need fine-tuning or a VLM (see the architecture docs).
- H.264 encoding isn't available to OpenCV on this machine, so annotated videos are written as MPEG-4 Part 2 (`mp4v`). They play in VLC, not in browsers. Installing ffmpeg would enable browser-playable output.
- Track quality metrics are proxies. A labelled clip from a real camera would allow proper MOTA/IDF1 scoring.

## Artifacts
`backend/benchmark_output/` (not committed): `results.json`, annotated videos for every combination, and the busiest annotated frames per clip in `frames/`.
