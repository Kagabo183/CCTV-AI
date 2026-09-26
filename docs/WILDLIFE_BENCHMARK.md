# Wildlife intelligence: benchmark and design

Measured on 25 Sep 2026 on the platform's wildlife documentary (YouTube `4nLTlnyez2I`, 56 min), using an RTX
PRO 4000 Blackwell and FP16.

Reproduce:

```
backend/scripts/wildlife_benchmark.py detect
backend/scripts/wildlife_benchmark.py species --detector MDV6-yolov10-e
backend/scripts/wildlife_benchmark.py final   --detector MDV6-yolov10-e
backend/scripts/wildlife_benchmark.py table
```

The hand labels are in `bench/wildlife/gt.json`. The raw outputs are in `bench/wildlife/*.json`. The full per-frame
table is `bench/wildlife/per_frame_table.md`.

## Problem

The general detector (YOLO26s, COCO) only knows ten animal classes. The documentary's hippos, rhinos, buffalo,
wildebeest and lion have no COCO class, so YOLO labels them with the nearest class it has: "cow", "elephant",
"dog", "bear" or "horse". A lower confidence threshold cannot fix a missing class.

## Test set

39 frames were checked by eye across the whole video. Each frame lists the species visibly present. Where the
animals could be counted exactly, the count is recorded too: 23 frames, 47 animals.

| Species | Frames |
|---|---|
| Elephant | 8 |
| Giraffe | 7 |
| Hippopotamus | 8 |
| Zebra | 7 |
| Rhinoceros | 5 |
| Buffalo | 4 |
| Wildebeest | 5 |
| Lion | 2 |
| Cheetah or leopard | 1 |

## 1. Animal detection

| Detector | Counted animals found, conf 0.25 | Extra boxes | Frames with an animal found | ms / frame | VRAM |
|---|---|---|---|---|---|
| YOLO26s tiled (current, COCO animal classes) | 38/47 (0.81) | 4 | 37/39 | 62.7 | 444 MB |
| MDV6-yolov9-c | 32/47 (0.68) | 24 | 34/39 | 11.1 | 126 MB |
| MDV6-yolov9-e (1280) | 39/47 (0.83) | 3 | 35/39 | 25.6 | 710 MB |
| MDV6-yolov10-c | 39/47 (0.83) | 6 | 33/39 | 6.9 | 43 MB |
| **MDV6-yolov10-e (1280)** | **43/47 (0.91)** | **2** | **37/39** | **16.3** | **288 MB** |
| MDV6-rtdetr-c | 44/47 (0.94) | 18 | 37/39 | 16.1 | 210 MB |
| YOLO26s trained on the African Wildlife dataset | 35/47 (0.74) | 3 | 29/39 | 8.3 | 64 MB |

At conf 0.5, MDV6-yolov10-e still finds 41/47 animals with 1 extra box. The other thresholds are in the script output.

"Extra boxes" is the number of boxes beyond the true count, in frames where the animals could be counted. It is a
proxy for false positives, since boxes were not drawn by hand.

**Chosen: MDV6-yolov10-e at conf 0.25.** It has the best balance: recall 0.91 with 2 extra boxes, at 16 ms per frame.
RT-DETR-c finds one more animal but adds 18 extra boxes. The model is loaded straight into our Ultralytics; the
PyTorch-Wildlife package is not needed.

## 2. Species identification (per detected animal, MDV6-yolov10-e boxes, the 8 largest per frame)

| Classifier | Correct | Uncertain | Wrong | n |
|---|---|---|---|---|
| Current YOLO26s (COCO labels, its own boxes) | 41% | 0% | 59% | 203 |
| YOLO26s trained on the African Wildlife dataset (4 classes) | 68% | – | 32% | 103 |
| Snapshot Serengeti ResNet-18 (10 classes) | 45% | 35% | 20% | 152 |
| SpeciesNet v4.0.3a, geofence RWA as shipped | 23% | 61% | 16% | 152 |
| SpeciesNet v4.0.3a, no geofence | 56% | 27% | 17% | 152 |

What the numbers show:

- **SpeciesNet** is the best general classifier here. It gets giraffes, zebras, elephants, lions, the cheetah and
  buffalo right, and often wildebeest. It has two failure modes:
  - It confidently calls rhinos, and some hippos and buffalo, **"domestic cattle"**, sometimes at 0.997.
  - Hippos often roll up to "animal" or "mammal" instead of a species.
- **The Rwanda geofence as shipped removes plains zebra, giraffe, white rhino and black rhino.** SpeciesNet's
  geofence file (release 2026-06-09) does not list Rwanda for these species, even though they live in Akagera.
  That is why accuracy with the geofence drops to 23%.
- **Snapshot Serengeti** only knows wildebeest, zebra, buffalo, impala, gazelles, warthog, hyena and guineafowl.
  Everything else is "other".
- **The African Wildlife model** (trained here: 40 epochs, mAP50 0.96 on its own test split) is the only model
  that names rhinos correctly (4 of 4 frames). It also calls hippos "rhino", so it cannot be trusted alone.

## 3. Final design and its measured result

```
General vision   YOLO26s (+ tiling)             people, vehicles, bags, everyday objects
Wildlife vision  MegaDetector V6 (yolov10-e)    "animal" boxes  -> ByteTrack  -> animal tracks
Species          SpeciesNet v4.0.3a on up to 4 crops per TRACK (not every frame), geofenced
                 + African-Wildlife YOLO as a second opinion only for uncertain animals
Rules            never force a species (below)
Events           zone / line / dwell / restricted area / infrastructure / group / fast movement
Understanding    local VLM or Gemini, only for "what is it doing / why" questions
```

Rules that turn classifier output into a label:

1. **Certain:** SpeciesNet at species level, not a domestic animal, with score ≥ 0.5. Shown as, for example,
   "hippopotamus".
2. **Domestic label:** a domestic label such as "domestic cattle" is never shown as fact in wildlife mode. It is
   shown as "possible X", where X is the specialist's answer, SpeciesNet's best wild candidate, or the domestic
   label itself.
3. **Roll-up:** if SpeciesNet rolls up to a genus, family, order, "animal" or "mammal", the label is uncertain.
   The candidate shown is SpeciesNet's own top species if its score is ≥ 0.3, else the specialist's answer, else
   "animal (species uncertain)".
4. **Per track:** a track is certain only if at least half of its crops agree on the same species. Otherwise it is
   "possible <most common guess>".

**Geographic context.** The geofence stays on, with `WILDLIFE_COUNTRY=RWA`. `WILDLIFE_GEOFENCE_ALLOW` adds
Rwanda to four species that SpeciesNet's geofence file leaves out:

- *Equus quagga* (plains zebra)
- *Giraffa camelopardalis* (giraffe)
- *Ceratotherium simum* (white rhino)
- *Diceros bicornis* (black rhino)

These are my additions from general knowledge of Akagera, not something the model data states. Check them against
the park's current species list, and adjust the list for other deployments. No probabilities are invented: the
override only tells SpeciesNet's own geofence that the species occurs in the country.

| Platform identifier (on MDV6-yolov10-e boxes) | Correct | Wrong | Uncertain, best guess right | Uncertain |
|---|---|---|---|---|
| SpeciesNet, no geofence + rules | 56% | 1% | 9% | 34% |
| SpeciesNet, RWA + allow-list + rules | 47% | 1% | 18% | 35% |
| **SpeciesNet, RWA + allow-list + specialist + rules (shipped)** | **47%** | **1%** | **26%** | **26%** |

**Confidently wrong labels drop from 59% (current YOLO) to 1%.** The rest are either correct or explicitly
uncertain. Wildebeest stay "possible common wildebeest", because the Rwanda geofence does not allow them.

Examples from the per-frame table:

| Time | Actual | Current YOLO | MegaDetector found | Platform label | Correct? |
|---|---|---|---|---|---|
| 1:30 | lion | cat ×1 | 1 (actual 1) | lion | yes |
| 17:50 | giraffe | giraffe ×1 | 2 (actual 2) | giraffe ×2 | yes |
| 19:30 | rhinoceros | bird ×2 | 1 (actual 1) | possible rhinoceros | uncertain (guess right) |
| 24:00 | hippopotamus | elephant ×1 | 1 (actual 1) | possible hippopotamus | uncertain (guess right) |
| 26:10 | hippopotamus | elephant ×6, cow ×4 | 9 | hippopotamus ×3; animal (species uncertain) ×2; possible rhinoceros ×1 | yes |
| 30:30 | buffalo | cow ×15 | 18 | african buffalo ×5; possible bovidae family; possible domestic cattle | yes |
| 34:30 | zebra | zebra ×2 | 2 (actual 2) | plains zebra ×2 | yes |
| 49:30 | rhinoceros | cow ×1 | 0 (actual 1) | no animal detected | – |
| 51:30 | hippopotamus | elephant ×1 | 1 (actual 2) | hippopotamus | yes |

## Full-video run

The wildlife run on the documentary took 6.4 minutes for the analysed 30 minutes, using parallel stream reading at
1.67 sampled fps. It classified 828 animal tracks.

| Species | Most at the same time |
|---|---|
| African elephant | 10 |
| Plains zebra | 10 |
| African buffalo | 8 |
| Hippopotamus | 3 |
| Giraffe | 3 |
| Black rhinoceros | 2 |
| Lion | 1 |
| Impala | 1 |

The impala is not in the labelled frames and was not checked. 614 tracks stayed "animal (species uncertain)",
mostly short or distant animals seen at the low sampling rate.

## Limits

- **Documentary footage is not camera-trap footage.** SpeciesNet was trained on camera traps. Close-ups, water and
  motion blur are harder for it, and so are the heavy herds. For real Rwandan cameras, re-run this benchmark on
  their footage.
- **Crowded herds.** The wildebeest crossings (180 animals in one frame) are counted by MegaDetector but mostly left
  uncertain.
- **Small test set.** The labelled set is 39 frames. The numbers show which approach is better; they are not
  population-level accuracy.
- **Unverified Kinyarwanda names.** The Kinyarwanda animal names used for translation (imvubu, inzovu, imparage,
  intwiga, inkura, imbogo, intare, ingwe) need review by a native speaker.
