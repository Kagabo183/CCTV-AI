"""Measure the camera platform under load: live AI throughput per camera, GPU and CPU use.

Run with the backend up and cameras connected (e.g. after scripts/camera_acceptance.py):
    python scripts/camera_perf.py [--seconds 60] [--label "sub-stream, no tiling"]
Appends to bench/camera_perf.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx
import psutil

API = "http://127.0.0.1:8000/api"


def gpu() -> dict[str, float] | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        util, used, total = (float(x) for x in out.split(","))
        return {"util_pct": util, "mem_used_mb": used, "mem_total_mb": total}
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return None


def procs() -> dict[str, psutil.Process]:
    found: dict[str, psutil.Process] = {}
    for p in psutil.process_iter(["name", "cmdline", "memory_info"]):
        cmd = " ".join(p.info["cmdline"] or [])
        key = ("backend" if "uvicorn" in cmd and "app.main" in cmd else
               "media_server" if p.info["name"] and "mediamtx" in p.info["name"].lower() and "camsim" not in cmd else None)
        # on Windows a venv python.exe is a launcher with the real interpreter as its child: keep the biggest
        if key and (key not in found or p.info["memory_info"].rss > found[key].memory_info().rss):
            found[key] = p
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    c = httpx.Client(base_url=API, timeout=30, trust_env=False)
    c.post("/auth/login", json={"email": "owner-acceptance@example.com", "password": "acceptance-pass-1"}).raise_for_status()
    cams = {x["id"].replace("-", ""): x for x in c.get("/cameras").json()}
    ps = procs()
    for p in ps.values():
        p.cpu_percent(None)
    first = c.get("/cameras-overview").json()["live_ai"]
    gpus = []
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        g = gpu()
        if g:
            gpus.append(g)
        time.sleep(2)
    dt = time.time() - t0
    last = c.get("/cameras-overview").json()
    cpu = {k: round(p.cpu_percent(None) / psutil.cpu_count(), 1) for k, p in ps.items()}
    rss = {k: round(p.memory_info().rss / 2**20) for k, p in ps.items()}
    rows = []
    for cid, v in last["live_ai"].items():
        before = first.get(cid, {"frames": 0})
        cam = cams.get(cid.replace("-", ""), {})
        stream = next((s for s in cam.get("streams", []) if s["role"] == "main"), {})
        rows.append({"camera": cam.get("name", cid[:8]), "detectors": v["detectors"], "target_fps": v["fps"],
                     "effective_fps": round((v["frames"] - before["frames"]) / dt, 2), "latency_ms": v["latency_ms"], "dropped": v["dropped"],
                     "main_stream": f"{stream.get('width')}x{stream.get('height')} {stream.get('codec')}"})
    result = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "label": a.label, "seconds": round(dt), "cameras_online": last["states"].get("online", 0),
              "live_ai_workers": len(rows), "cpu_pct_of_machine": cpu, "rss_mb": rss, "cpu_cores": psutil.cpu_count(),
              "gpu": {"util_pct_mean": round(sum(g["util_pct"] for g in gpus) / len(gpus), 1), "mem_used_mb": gpus[-1]["mem_used_mb"],
                      "mem_total_mb": gpus[-1]["mem_total_mb"]} if gpus else None,
              "viewers": last["viewers"], "cameras": rows}
    out = Path(__file__).resolve().parents[2] / "bench/camera_perf.json"
    history = json.loads(out.read_text()) if out.exists() else []
    history.append(result)
    out.write_text(json.dumps(history, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "cameras"}, indent=1))
    for r in rows:
        print(f"  {r['camera'][:28]:28} {'+'.join(r['detectors']):9} {r['effective_fps']:>5} fps (target {r['target_fps']})  {r['latency_ms']} ms/frame  {r['main_stream']}")


if __name__ == "__main__":
    main()
