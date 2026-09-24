# Detector diagnosis: "person 0.66" on a rabbit

**Test video:** *Big Buck Bunny* (the "HLS test stream" and "mov_bbb.mp4" sources). It's an animated film with a large white rabbit, a flying squirrel, birds and an apple.
**Method:** the detectors were run **directly** on saved frames, bypassing the app (`scripts/detector_diagnosis.py`). Raw outputs are in `backend/diagnosis_output*/raw.json` and annotated images are next to them. The "Actual" column was written by a human after looking at each frame. No results were edited.

## Root cause: both A and B

**A: the application filtered classes.** `detectors.py` restricted every detector to 9 hard-coded "CCTV" classes (person, bicycle, car, motorcycle, bus, truck, backpack, handbag, suitcase), passed as `model.predict(classes=[0,1,2,3,5,7,24,26,28])`. Correct detections were thrown away: "bird 0.64" (t=25 s) and "apple 0.87" (t=90 s) never reached the app. **Fixed:** no filter by default. `VISION_CLASSES` restricts classes only when you set it.

**B: the model itself misclassifies the rabbit.** With no filter at all, YOLO26s (COCO) still says **"person 0.66"** on the same frame (t=66.6 s). **COCO has no rabbit class**, and an upright cartoon rabbit looks most like a person to a model trained on photos. Objects365 *does* have a "rabbit" class, yet still says "person 0.80" on the rabbit (t=213 s) and "cat 0.63" at t=60 s. This isn't a bug we can fix in code. It's the limit of a closed-set detector on content outside its training distribution.

## Frame-by-frame results

| t | Actual (human) | COCO, old app filter | COCO, all 80 classes | Objects365 (365) | Correct? | Local VLM (Qwen3-VL 8B) | Gemini |
|---|---|---|---|---|---|---|---|
| 4 s | title card text | – | – | traffic sign 0.77 | O365 ✗ (false positive) | ✓ "animated text 'Big Buck Bunny'" | |
| 12 s | landscape, tiny speck | person 0.29 | person 0.29 | person 0.46 | ✗ (low-conf guess) | ✓ landscape, trees, hills | |
| 25 s | purple cartoon bird | **nothing** | **bird 0.64** ✓ | wild bird 0.28 | filter hid it | ✓ "purple bird… holding an acorn" | |
| 40 s | dark burrow, rabbit inside | – | sports ball 0.78 (rocks) | elephant 0.34 | ✗ | ✗ "a large dark bear… curled up in a hole" | |
| 60 s | white rabbit by burrow | – | – | cat 0.63 | ✗ | ✓ "large light gray animated rabbit" | ✗ "only sky" (sampled the next shot) |
| 66.6 s | rabbit close-up | **person 0.66** | **person 0.66** | – | ✗ (the reported bug) | ✓ "large white animated rabbit" | ✓ "large animated white rabbit" |
| 90 s | rabbit, red apple, butterflies | – | apple 0.87 ✓, bird 0.30–0.44 | apple 0.84 ✓, wild bird 0.52 (on the rabbit) | apple ✓, rabbit ✗ | ✓ rabbit + red apple | |
| 150 s | flying squirrel on a tree | – | cow 0.76 | giraffe 0.32, cow 0.27 | ✗ | ✓ "brown cartoon squirrel clinging to the trunk" | ✓ "animated squirrel clinging to a tree trunk" |
| 213 s | rabbit in a field | person 0.94 | person 0.94 | person 0.80 | ✗ (high confidence!) | ✓ "large white animated rabbit" | |
| 300 s | tree bark, no animal | – | giraffe 0.39 | giraffe 0.45 | ✗ (bark pattern) | ✓ tree trunk, bark, leaf shadows | |
| 319.5 s | rabbit's paws with a bow | person 0.66 | person 0.66, scissors 0.46 | person 0.59 | ✗ | ✓ "white rabbit holding a bow and arrow" | |
| 407.9 s | flying squirrel gliding | person 0.94 | person 0.94 | person 0.96 | ✗ | ~ "brown and white furry character with wings" | ✓ "animated flying squirrel" |
| 520 s | credits + squirrel | person 0.93 | person 0.93 | person 0.94 | ✗ | ✓ squirrel + credits text | |

**Takeaways**
- On this cartoon, the detectors identified the main subject correctly in 1 of 13 frames (the bird, COCO unfiltered; Objects365 got it too, at only 0.28). They were wrong with **high confidence** several times ("person 0.94" on the rabbit). Confidence thresholds alone therefore cannot catch every error, which is why a VLM path is needed.
- The vision-language models named the actual subject in most frames. They still made mistakes: the local VLM said "bear" for the dark burrow, and Gemini missed the moment at t=60 s because it samples 1 frame per second.
- This is animated footage, far from the photos these detectors were trained on. On real photos they do much better (next section). Real CCTV footage from your cameras is the test that matters.

## Class coverage on real photos (COCO YOLO26s)

`scripts/class_coverage.py`, run on 128 labelled COCO photos (coco128). **Caveat:** these images come from COCO's *training* split, so the numbers are optimistic.

- 80 classes in the model; **68 distinct classes detected**.
- Found reliably: person 57/61, dog 7/9, cat 4/4, bird 2/2, horse 1/1, bear 1/1, elephant 4/4, giraffe 4/4, zebra 2/2, bottle 6/6, chair 8/9, cup 10/10, suitcase 2/2, motorcycle 4/4, bus 4/5, truck 3/5, car 9/12.
- Weak on small hand-held objects: cell phone 1/5, fork 1/6, handbag 4/9, backpack 2/4, laptop 1/2, mouse 0/2.
- Not present in coco128, so not measured: sheep, cow.

**Objects365 (YOLO26s-O365):** 365 classes in the model, **123 distinct classes** detected on the same photos. That's broader vocabulary (sneakers, hat, lamp, street lights, flag, helmet, …, and it includes rabbit, deer, monkey and more), at about 60% of COCO's speed. On the rabbit video it was no more accurate: broader vocabulary doesn't fix animated content.

## What changed in the system

1. **No hidden class filter.** Detectors return every class the model knows. `VISION_CLASSES` (e.g. `person,car`) restricts them only when you set it. `GET /api/vision/models` lists each detector's architecture, dataset and full class list. Every model load is logged with its model card; set the `app.vision.inference` logger to DEBUG to log every inference (class IDs, names, confidences).
2. **Objects365 detector:** `VISION_DETECTOR=yolo_o365` (`yolo26s-objv1-150.pt`). It's also in the Vision lab, and "Compare detectors" runs COCO, Objects365 and RT-DETR side by side.
3. **Unknown / uncertain state.** A track whose mean confidence is below `VISION_CONFIRM_CONFIDENCE` (default 0.5), or whose class keeps changing (< 60% agreement), is stored as `{"class": "unknown", "candidate_class": "person", "confidence": 0.41, "requires_vlm": true}`:
   - Events call it an "unidentified object (possibly person, 0.41)".
   - Person-only rules (loitering, crowd, person entered) do not fire on it.
   - The overlay draws it dashed as `? person 0.41`.
4. **Open-ended understanding.** `VideoUnderstandingProvider` → `GeminiVideoProvider`, `LocalVLMProvider` (any OpenAI-compatible server: Ollama, LM Studio, vLLM), `TogetherVideoProvider`. They're tried in order (`VIDEO_UNDERSTANDING_PROVIDERS=gemini,local_vlm`), so the local VLM covers for Gemini when the quota runs out. The agent uses them for "what is that?", "what is it doing?", "what changed?", and for unknown objects (`list_uncertain_objects` tool). Answers are always marked `model_interpretation`.
5. **UI.** The **Objects detected** panel shows counts for every class, including Unknown. Click an object to inspect its class, candidate, confidence, class votes, time range, track ID and box; to play its segment; or to **ask the video AI what it is**. That answer is stored as a model interpretation and never overwrites the detector's label.

## Limits

- Closed-set detectors cannot detect "any object". Objects365's 365 classes are still a fixed list.
- High-confidence mistakes (person 0.94 on a rabbit) are not caught by the uncertainty rule. Only a VLM check or fine-tuning on your own footage fixes those.
- VLMs can also be wrong (the "bear"). Their answers are shown as interpretations, not facts.
- The Together AI provider is implemented but untested here, because no API key is configured.
