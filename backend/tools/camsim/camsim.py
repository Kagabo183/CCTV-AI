"""Simulated IP cameras for testing (a camera-side media server + looping publishers + the ONVIF simulator).

    python tools/camsim/camsim.py            # start everything; Ctrl+C stops it
    python tools/camsim/camsim.py --no-onvif
    python tools/camsim/camsim.py --kill Streaming/Channels/101      # simulate a camera going offline
    python tools/camsim/camsim.py --restart Streaming/Channels/101   # ... and coming back

RTSP (user admin / password Cam#2026!), served by a separate MediaMTX on :18554:
    Streaming/Channels/101   street, H.264 1080p main stream       (ONVIF camera :18080, Profile T)
    Streaming/Channels/102   street, H.264 360p sub stream
    h265/main                parking, H.265                        (RTSP-only camera)
    nvr/ch1 .. nvr/ch4       street-sub / corridor / plaza / wildlife  (ONVIF NVR :18081)
    wild/main                wildlife waterhole (hippos, zebras)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
MEDIAMTX = BACKEND / "tools" / "mediamtx" / "mediamtx.exe"
MEDIA = HERE / "media"
DOWN = MEDIA / "down"  # marker files: a camera whose marker exists is switched off
PATHS = {
    "Streaming/Channels/101": "street_main.mp4",
    "Streaming/Channels/102": "street_sub.mp4",
    "h265/main": "parking_h265.mp4",
    "nvr/ch1": "street_sub.mp4",
    "nvr/ch2": "corridor.mp4",
    "nvr/ch3": "plaza.mp4",
    "nvr/ch4": "wildlife.mp4",
    "wild/main": "wildlife.mp4",
}
CONFIG = """
logLevel: warn
rtspAddress: 127.0.0.1:18554
rtspTransports: [tcp]
rtmp: false
hls: false
webrtc: false
srt: false
moq: false
api: true
apiAddress: 127.0.0.1:19997
authMethod: internal
authInternalUsers:
  - user: admin
    pass: "Cam#2026!"
    permissions: [{action: read}]
  - user: publisher
    pass: simpublish
    ips: ["127.0.0.1"]
    permissions: [{action: publish}]
  - user: any
    ips: ["127.0.0.1"]
    permissions: [{action: api}]
paths:
  all_others:
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-onvif", action="store_true")
    ap.add_argument("--kill", metavar="PATH")
    ap.add_argument("--restart", metavar="PATH")
    a = ap.parse_args()
    DOWN.mkdir(exist_ok=True)
    if a.kill or a.restart:  # talk to the running simulator through marker files
        marker = DOWN / (a.kill or a.restart).replace("/", "_")
        marker.touch() if a.kill else marker.unlink(missing_ok=True)
        return
    for m in DOWN.iterdir():
        m.unlink()
    sys.path.insert(0, str(BACKEND))
    from app.video import ffmpeg

    cfg = HERE / "camsim.yml"
    cfg.write_text(CONFIG)
    procs = [subprocess.Popen([str(MEDIAMTX), str(cfg)])]
    time.sleep(1.5)

    def publish(path: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen([ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", str(MEDIA / PATHS[path]),
                                 "-c", "copy", "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://publisher:simpublish@127.0.0.1:18554/{path}"])

    cams = {path: publish(path) for path in PATHS}
    if not a.no_onvif:
        procs.append(subprocess.Popen([sys.executable, str(HERE / "onvif_sim.py")]))
    print("simulated cameras running: rtsp://admin:***@127.0.0.1:18554/<path>; ONVIF on :18080 (camera) and :18081 (NVR)", flush=True)
    try:
        while all(p.poll() is None for p in procs):
            for path, proc in list(cams.items()):
                down = (DOWN / path.replace("/", "_")).exists()
                if down and proc.poll() is None:
                    proc.terminate()
                    print(f"camera {path}: offline", flush=True)
                elif not down and proc.poll() is not None:
                    cams[path] = publish(path)
                    print(f"camera {path}: online", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in [*procs, *cams.values()]:
            p.terminate()


if __name__ == "__main__":
    main()
