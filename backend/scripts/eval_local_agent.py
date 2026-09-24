"""Score a local assistant configuration on questions with known answers.

    python scripts/eval_local_agent.py --agent qwen2.5:14b --vlm qwen2.5vl:7b [--source <id>]

Runs the real agent (NLLB translation, tools over stored vision data, local VLM)
against the street test video and checks each English answer against facts taken
from the stored tracking data (not from any model).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

# (question in Kinyarwanda, check on the English answer, what a correct answer contains)
CASES = [
    ("Ni abantu bangahe bagaragaye kuri iyi camera icyarimwe?", lambda a: re.search(r"\b7\b|seven", a, re.I), "7 people at the same time"),
    ("Ni imodoka zingahe zagaragaye icyarimwe?", lambda a: re.search(r"\b8\b|eight", a, re.I), "8 cars at the same time"),
    ("Ni iki kiri mu idirishya ry'iduka ryitwa Wig Center?", lambda a: re.search(r"wig|mannequin|head", a, re.I), "wigs on mannequin heads"),
    ("Hari amatara yo ku muhanda agaragara?", lambda a: re.search(r"traffic light|yes", a, re.I), "yes, traffic lights (9 tracked)"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="qwen2.5:14b")
    ap.add_argument("--vlm", default="qwen2.5vl:7b")
    ap.add_argument("--source", default="112a9e1807c54a03b8df7c2f5414a7b9")
    ap.add_argument("--repeat", type=int, default=2, help="run each question N times (small models are inconsistent)")
    args = ap.parse_args()
    os.environ.update(AGENT_LLM="local", AGENT_LOCAL_MODEL=args.agent, LOCAL_VLM_MODEL=args.vlm, VIDEO_UNDERSTANDING_PROVIDERS="local_vlm",
                      TRANSLATION_PROVIDER="nllb", VISION_ENABLED="true", AGENT_LOCAL_REASONING="", LOCAL_VLM_REASONING="")

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.analyzers.base import AnalysisQuery, PreparedVideo, SourceContext
    from app.analyzers.factory import get_video_analyzer
    from app.db.session import override_engine

    override_engine(create_async_engine("sqlite+aiosqlite:///./dev.db"))
    sid = uuid.UUID(args.source)
    owner = sqlite3.connect("dev.db").execute("select owner_id from video_sources where id=?", (sid.hex,)).fetchone()[0]
    uid = uuid.UUID(hex=owner) if "-" not in owner else uuid.UUID(owner)

    async def run() -> None:
        agent = get_video_analyzer()
        score, total, times = 0, 0, []
        for question, check, expected in CASES:
            for _ in range(args.repeat):
                t = time.time()
                try:
                    r = await agent.analyze(PreparedVideo(analyzer="agent", ref={"local": True}),
                                            AnalysisQuery(question=question, language="rw", user_id=uid, source=SourceContext(source_id=sid, name="street video", kind="upload", duration_seconds=89)))
                    en, rw, tools = r.trace.get("answer_en", r.answer), r.answer, r.trace.get("tools_used")
                except Exception as exc:  # noqa: BLE001
                    en, rw, tools = f"ERROR {getattr(exc, 'message', exc)}", "", []
                dt = time.time() - t
                ok = bool(check(en))
                score += ok
                total += 1
                times.append(dt)
                print(f"{'PASS' if ok else 'FAIL'} {dt:5.1f}s | Q: {question}\n      expected: {expected}\n      EN: {en}\n      RW: {rw}\n      tools: {tools}")
        print(f"\n{args.agent} + {args.vlm}: {score}/{total} correct, median {sorted(times)[len(times) // 2]:.1f}s, max {max(times):.1f}s")

    asyncio.run(run())


if __name__ == "__main__":
    main()
