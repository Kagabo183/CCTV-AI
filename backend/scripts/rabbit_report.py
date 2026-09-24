"""Frame-by-frame detector vs. vision-language model report on the rabbit test video.

Inputs: diagnosis_output/raw.json and diagnosis_person/raw.json (from detector_diagnosis.py).
Asks the configured VLMs about each frame and writes docs/DETECTOR_DIAGNOSIS_FRAMES.json.
"Actual" is what a human sees in the frame (written by the reviewer, not by a model).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.analyzers.base import PreparedVideo, SourceContext  # noqa: E402
from app.understanding.base import UnderstandingRequest, VideoRef  # noqa: E402
from app.understanding.providers import GeminiVideoProvider, LocalVLMProvider  # noqa: E402

VIDEO = Path("uploads/2e04398f-6a35-4efe-aeea-aae2f8997248/540701c3-fbae-48bd-aee1-d1d42f986f14.mp4")
# Human-verified contents of each frame (reviewed from the saved images).
ACTUAL = {
    4.0: "title card: 'Big Buck Bunny' logo text, no object",
    12.0: "landscape: trees and hills; a tiny distant speck in the field",
    25.0: "purple cartoon bird on a branch",
    40.0: "dark burrow among rocks and grass; rabbit barely visible inside",
    60.0: "large white cartoon rabbit standing by its burrow",
    66.6: "close-up of the white rabbit's face and upper body",
    90.0: "white rabbit sitting in a meadow, red apple, small butterflies",
    150.0: "brown cartoon flying squirrel clinging to a tree trunk",
    213.0: "white rabbit standing in a field",
    300.0: "close-up of a tree trunk, no animal",
    319.5: "close-up of the rabbit's paws holding a stick/bow",
    407.9: "flying squirrel gliding through the sky",
    520.0: "end credits text, with a flying squirrel image",
}
QUESTION = "What is the main subject of this frame? Name the object or animal precisely (it may be animated/cartoon) and say what else is visible."


async def main() -> None:
    frames = {}
    for folder in ("diagnosis_output", "diagnosis_person"):
        for f in json.loads(Path(folder, "raw.json").read_text())["frames"]:
            frames[round(f["t"], 1)] = f["results"]
    local = LocalVLMProvider(base_url="http://127.0.0.1:11434/v1", model="qwen3-vl:8b", frames=1)
    gemini = None
    if "--gemini" in sys.argv:
        from app.analyzers.gemini import GeminiVideoAnalyzer
        from app.core.config import get_settings

        s = get_settings()
        analyzer = GeminiVideoAnalyzer(api_key=s.gemini_api_key.get_secret_value(), model=s.gemini_model)
        import sqlite3

        ref = json.loads(sqlite3.connect("dev.db").execute(
            "select provider_ref from video_sessions s join video_sources v on v.id = s.video_source_id where v.name='HLS test stream' and s.status='ready' order by s.created_at desc"
        ).fetchone()[0])
        gemini = (GeminiVideoProvider(analyzer), PreparedVideo(analyzer="gemini", ref=ref))
    source = SourceContext(name="Big Buck Bunny test video", kind="url")
    out = []
    for t in sorted(frames):
        request = UnderstandingRequest(question=QUESTION, source=source, start_seconds=t, end_seconds=t + 0.2)
        row = {"t": t, "actual": ACTUAL.get(t, "?"), "detectors": {k: [f"{d['class']} {d['confidence']:.2f}" for d in v[:4]] for k, v in frames[t].items()}}
        try:
            r = await local.understand(VideoRef(local_path=VIDEO), request)
            row["local_vlm"] = r.description
        except Exception as exc:  # noqa: BLE001
            row["local_vlm"] = f"ERROR: {exc}"
        if gemini and t in (60.0, 66.6, 150.0, 407.9):
            try:
                r = await gemini[0].understand(VideoRef(prepared=gemini[1]), request.model_copy(update={"start_seconds": t, "end_seconds": t + 1.0}))
                row["gemini"] = r.description
            except Exception as exc:  # noqa: BLE001
                row["gemini"] = f"ERROR: {getattr(exc, 'message', exc)}"
        out.append(row)
        print(f"t={t:6.1f} | actual: {row['actual']}\n         coco_filtered={row['detectors']['coco_filtered']} coco_all={row['detectors']['coco_all']} o365={row['detectors']['objects365']}\n         local VLM: {row['local_vlm'][:220]}")
        if "gemini" in row:
            print(f"         Gemini: {row['gemini'][:220]}")
    Path("../docs").mkdir(exist_ok=True)
    Path("diagnosis_output/frames_report.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
