"""Camera platform acceptance run against the simulated cameras (tools/camsim/camsim.py).

Drives the real API (backend on :8000, media server started by it) and prints one line per check plus a
JSON summary (bench/camera_acceptance.json). Usage:

    python tools/camsim/camsim.py            # in one terminal
    uvicorn app.main:app --port 8000         # in another (CAMERA_PRIVATE_NETWORKS must include 127.0.0.0/8)
    python scripts/camera_acceptance.py [--only rtsp,onvif,...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

API = "http://127.0.0.1:8000/api"
SIM = "rtsp://admin:Cam%232026!@127.0.0.1:18554"
PASSWORD = "Cam#2026!"
results: list[dict] = []


def check(name: str, ok: bool, detail: str = "", **data: object) -> bool:
    results.append({"check": name, "ok": ok, "detail": detail, **data})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def user(client: httpx.Client, email: str) -> httpx.Client:
    c = httpx.Client(base_url=API, timeout=120, trust_env=False)
    body = {"email": email, "password": "acceptance-pass-1", "name": email.split("@")[0]}
    r = c.post("/auth/register", json=body)
    if r.status_code != 201:
        r = c.post("/auth/login", json={"email": email, "password": body["password"]})
    r.raise_for_status()
    return c


def wait_online(c: httpx.Client, cam_id: str, timeout: float = 45) -> tuple[bool, float, dict]:
    t0 = time.time()
    st: dict = {}
    while time.time() - t0 < timeout:
        st = c.get(f"/cameras/{cam_id}/status").json()
        if st.get("media") and st["media"].get("ready"):
            return True, time.time() - t0, st
        time.sleep(0.5)
    return False, time.time() - t0, st


def hls_read(c: httpx.Client, cam_id: str) -> tuple[bool, str]:
    """Fetch the HLS playlist with the live token: exercises the media server auth hook end to end."""
    live = c.get(f"/cameras/{cam_id}/live").json()
    url = "http://127.0.0.1:8888" + live["hls_url"].removeprefix("/media/hls")
    # MediaMTX answers the first HLS request with a cookie check redirect; browsers pass it transparently
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=True, headers={"Cookie": "cookieCheck=1"}) as raw:
        for _ in range(20):
            r = raw.get(url)
            if r.status_code == 200 and "#EXTM3U" in r.text:
                return True, f"HLS playlist ok ({live['stream']} stream), token expires in {live['expires_in']} s"
            time.sleep(1)
        return False, f"HLS status {r.status_code}"


def whep(c: httpx.Client, cam_id: str, h265: bool = True) -> tuple[bool, str, float | None]:
    """A real WebRTC WHEP session (aiortc) through the media server: time to first decoded frame."""
    try:
        import asyncio

        from aiortc import RTCPeerConnection, RTCSessionDescription
    except ImportError:
        return False, "aiortc not installed: WebRTC checked in the browser instead", None
    live = c.get(f"/cameras/{cam_id}/live", params={"h265": str(h265).lower()}).json()
    url = "http://127.0.0.1:8889" + live["whep_url"].removeprefix("/media/webrtc")

    async def run() -> tuple[bool, str, float | None]:
        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        got: asyncio.Future = asyncio.get_running_loop().create_future()

        @pc.on("track")
        def on_track(track):  # type: ignore[no-untyped-def]
            async def first() -> None:
                frame = await track.recv()
                if not got.done():
                    got.set_result(frame)
            asyncio.ensure_future(first())

        t0 = time.time()
        await pc.setLocalDescription(await pc.createOffer())
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            r = await client.post(url, content=pc.localDescription.sdp, headers={"Content-Type": "application/sdp"})
        if r.status_code != 201:
            await pc.close()
            return False, f"WHEP status {r.status_code}", None
        await pc.setRemoteDescription(RTCSessionDescription(sdp=r.text, type="answer"))
        try:
            frame = await asyncio.wait_for(got, 20)
            dt = time.time() - t0
            return True, f"first WebRTC frame {frame.width}x{frame.height} after {dt:.2f} s", dt
        except TimeoutError:
            return False, "no WebRTC frame in 20 s", None
        finally:
            await pc.close()

    return asyncio.run(run())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = set(filter(None, args.only.split(",")))
    want = lambda k: not only or k in only  # noqa: E731
    owner = user(httpx.Client(), "owner-acceptance@example.com")
    viewer = user(httpx.Client(), "viewer-acceptance@example.com")
    for cam in owner.get("/cameras").json():  # a clean slate for this run
        if cam["kind"] in ("nvr",) or not cam["parent_id"]:
            owner.delete(f"/cameras/{cam['id']}")
    made: dict[str, str] = {}

    if want("diagnosis"):
        cases = [
            ("OK", {"type": "rtsp", "url": f"{SIM}/Streaming/Channels/101"}, None),
            ("INVALID_CREDENTIALS", {"type": "rtsp", "url": "rtsp://127.0.0.1:18554/Streaming/Channels/101"}, None),
            ("AUTHENTICATION_FAILED", {"type": "rtsp", "url": "rtsp://admin:wrong@127.0.0.1:18554/Streaming/Channels/101"}, None),
            ("STREAM_NOT_FOUND", {"type": "rtsp", "url": f"{SIM}/no/such/path"}, None),
            ("RTSP_DISABLED", {"type": "rtsp", "url": "rtsp://admin:x@127.0.0.1:18599/x"}, None),
            ("DNS_FAILED", {"type": "rtsp", "url": "rtsp://camera-does-not-exist.invalid/x"}, None),
            ("AUTHENTICATION_FAILED", {"type": "onvif", "xaddr": "127.0.0.1:18080"}, ("admin", "nope")),
            ("ONVIF_DISABLED", {"type": "onvif", "xaddr": "127.0.0.1:18099"}, ("admin", PASSWORD)),
        ]
        for expect, spec, creds in cases:
            body = {"connection": spec, **({"username": creds[0], "password": creds[1]} if creds else {})}
            t0 = time.time()
            r = owner.post("/cameras/test-connection", json=body).json()
            got = r.get("diagnosis") or r.get("code")
            check(f"diagnosis {spec['type']} -> {expect}", got == expect, f"{got}: {r.get('explanation', r.get('detail', ''))[:90]} ({time.time() - t0:.1f} s)")
        r = owner.post("/cameras/test-connection", json={"connection": {"type": "rtsp", "url": "rtsp://127.0.0.1:8554/cam/x/main"}})
        check("loopback guard: Visionary's own media port refused", r.status_code == 422, r.json().get("detail", "")[:90])
        r = owner.post("/cameras/test-connection", json={"connection": {"type": "rtsp", "url": "rtsp://10.1.2.3/stream"}})
        check("not-allowed private network refused", r.status_code == 422 and r.json().get("code") == "not_allowed_network", r.json().get("detail", "")[:90])

    if want("discover"):
        t0 = time.time()
        d = owner.post("/cameras/discover", json={"timeout": 3}).json()
        names = [x["name"] for x in d.get("devices", [])]
        check("ONVIF discovery (authorised networks only)", len(names) >= 2, f"{names} on {d.get('networks')} in {time.time() - t0:.1f} s")

    if want("rtsp"):  # acceptance 1 + 3 (H.264)
        r = owner.post("/cameras", json={"name": "Street (RTSP)", "location": "irembo", "connection": {"type": "rtsp", "url": f"{SIM}/Streaming/Channels/101",
                                          "sub_url": f"{SIM}/Streaming/Channels/102"}})
        ok = check("1. add RTSP camera", r.status_code == 201, str(r.status_code) + " " + r.text[:120])
        if ok:
            cam = r.json()[0]
            made["rtsp"] = cam["id"]
            check("12. no credentials in API response", PASSWORD not in r.text and "Cam%23" not in r.text and "admin:" not in r.text, "response scanned for password/userinfo")
            online, dt, st = wait_online(owner, cam["id"])
            check("3. H.264 live via media server", online and "H264" in str(st.get("media", {}).get("tracks")), f"tracks={st.get('media', {}).get('tracks')} ready after {dt:.1f} s", seconds=dt)
            ok, msg = hls_read(owner, cam["id"])
            check("live view token (HLS) accepted", ok, msg)
            ok, msg, first = whep(owner, cam["id"])
            check("WebRTC WHEP playback", ok, msg, seconds=first)
            with httpx.Client(timeout=20, trust_env=False, follow_redirects=True, headers={"Cookie": "cookieCheck=1"}) as raw:
                r2 = raw.get(f"http://127.0.0.1:8888/cam/{uuid.UUID(cam['id']).hex}/sub/index.m3u8")
            check("stream without token refused", r2.status_code in (401, 403), f"status {r2.status_code}")
            r3 = viewer.get(f"/cameras/{cam['id']}/live")
            check("other user cannot see camera", r3.status_code == 404, f"status {r3.status_code}")
            owner.post(f"/cameras/{cam['id']}/shares", json={"email": "viewer-acceptance@example.com", "role": "viewer"})
            r4 = viewer.get(f"/cameras/{cam['id']}/live")
            r5 = viewer.patch(f"/cameras/{cam['id']}", json={"name": "hacked"})
            check("VIEWER share: live yes, settings no", r4.status_code == 200 and r5.status_code == 403, f"live {r4.status_code}, settings {r5.status_code}")
            snap = owner.get(f"/cameras/{cam['id']}/snapshot")
            check("snapshot", snap.status_code == 200 and snap.content[:2] == b"\xff\xd8", f"{len(snap.content)} bytes")

    if want("h265"):  # acceptance 4
        r = owner.post("/cameras", json={"name": "Parking (H.265)", "connection": {"type": "rtsp", "url": f"{SIM}/h265/main"}})
        if check("4. add H.265 camera", r.status_code == 201, r.text[:160]):
            cam = r.json()[0]
            made["h265"] = cam["id"]
            online, dt, st = wait_online(owner, cam["id"])
            check("4. H.265 live via media server", online and "H265" in str(st.get("media", {}).get("tracks")), f"tracks={st.get('media', {}).get('tracks')} after {dt:.1f} s")
            ok, msg, _ = whep(owner, cam["id"], h265=False)
            check("4. H.265 camera in a browser without H.265 WebRTC (on-demand H.264 copy)", ok, msg)

    if want("onvif"):  # acceptance 2
        r = owner.post("/cameras", json={"name": "Gate (ONVIF)", "connection": {"type": "onvif", "xaddr": "127.0.0.1:18080"}, "username": "admin", "password": PASSWORD})
        if check("2. add ONVIF camera", r.status_code == 201, r.text[:160]):
            cam = r.json()[0]
            made["onvif"] = cam["id"]
            caps = cam["capabilities"]
            check("2. ONVIF Profile T/Media2 + streams", "T" in (caps.get("onvif_profiles") or []) and {s["role"] for s in cam["streams"]} >= {"main", "sub"},
                  f"profiles={caps.get('onvif_profiles')} {cam['device'].get('manufacturer')} {cam['device'].get('model')} streams={[s['role'] + ':' + str(s.get('width')) for s in cam['streams']]}")
            online, dt, _ = wait_online(owner, cam["id"])
            check("2. ONVIF live", online, f"ready after {dt:.1f} s")

    if want("nvr"):  # acceptance 7
        r = owner.post("/cameras", json={"name": "Office NVR", "connection": {"type": "nvr", "xaddr": "127.0.0.1:18081"}, "username": "admin", "password": PASSWORD})
        if check("7. add NVR", r.status_code == 201, f"{len(r.json()) if r.status_code == 201 else r.text[:160]} records"):
            chans = [c for c in r.json() if c["kind"] == "nvr_channel"]
            made["nvr"] = r.json()[0]["id"]
            check("7. NVR channels became cameras", len(chans) == 4, ", ".join(c["name"] for c in chans))
            ready = [wait_online(owner, c["id"])[0] for c in chans]
            check("7. every channel live", all(ready), f"{sum(ready)}/{len(chans)} online")

    if want("wildlife"):  # acceptance 8
        r = owner.post("/cameras", json={"name": "Waterhole", "location": "akagera", "connection": {"type": "rtsp", "url": f"{SIM}/wild/main"},
                                         "ai_profile": {"general": False, "wildlife": True, "species": True}})
        if check("8. add wildlife camera", r.status_code == 201, r.text[:120]):
            made["wildlife"] = r.json()[0]["id"]

    if want("multi"):  # acceptance 11
        ids = [v for k, v in made.items() if k != "nvr"]
        st = owner.get("/cameras-overview").json()
        check("11. simultaneous streams", st["states"].get("online", 0) >= len(ids), json.dumps(st["states"]))
        t0 = time.time()
        ok = [whep(owner, i, h265=False)[0] for i in ids[:3]]
        check("11. parallel viewers", all(ok), f"{sum(ok)}/{len(ok)} WebRTC sessions in {time.time() - t0:.1f} s")

    if want("ai") and made:
        time.sleep(40)  # let live AI run
        ov = owner.get("/cameras-overview").json()
        for k, v in ov["live_ai"].items():
            check(f"live AI running {k[:8]}", v["running"] and v["frames"] > 10, f"{v['frames']} frames, {v['fps']} fps target, {v['latency_ms']} ms/frame, detectors={v['detectors']}, dropped={v['dropped']}")
        for key in ("rtsp", "wildlife"):
            if key in made:
                ev = owner.get(f"/cameras/{made[key]}/events", params={"minutes": 20}).json()
                check(f"10. events in the last 20 minutes ({key})", len(ev) > 0, "; ".join(e["description"] for e in ev[:3])[:200], events=len(ev))

    if want("ai") and "rtsp" in made:  # recording + playback of the past
        from datetime import datetime, timedelta

        rec = owner.get(f"/cameras/{made['rtsp']}/recordings").json()
        spans = rec.get("spans", [])
        ok = bool(spans) and all(set(s) == {"start", "duration"} for s in spans)
        if ok:
            start = datetime.fromisoformat(spans[-1]["start"]) + timedelta(seconds=max(0.0, spans[-1]["duration"] - 12))
            t0 = time.time()
            r = owner.get(f"/cameras/{made['rtsp']}/playback", params={"start": start.isoformat(), "duration": 10})
            ok = r.status_code == 200 and r.headers.get("content-type") == "video/mp4" and len(r.content) > 10000
            check("recording + playback of the last seconds", ok, f"{len(spans)} span(s), {len(r.content) // 1024} KB MP4 in {time.time() - t0:.2f} s")
        else:
            check("recording + playback of the last seconds", False, f"spans={spans}")

    if want("assistant") and "rtsp" in made:  # acceptance 9 + 10: talk to live cameras
        owner.patch(f"/cameras/{made['rtsp']}", json={"name": "Camera 3"})
        start_on = made.get("h265") or made["rtsp"]  # start on another camera: "Camera 3" in the question switches to it
        questions = [("en", "What is happening on Camera 3?", {"get_current_objects"}),
                     ("en", "What happened on Camera 3 in the last 20 minutes?", {"get_recent_events", "count_objects"}),
                     ("rw", "Ni iki kiri kubera kuri Camera 3 ubu?", {"get_current_objects"}),
                     ("rw", "Ni iki cyabaye kuri Camera 3 mu minota 20 ishize?", {"get_recent_events", "count_objects"})]
        for lang, q, tools in questions:
            conv = owner.post("/conversations", json={"video_source_id": start_on, "language": lang}).json()
            t0 = time.time()
            r = owner.post(f"/conversations/{conv['id']}/messages", json={"question": q}, timeout=300)
            dt = time.time() - t0
            if r.status_code != 200:
                check(f"9/10. assistant: {q}", False, f"{r.status_code} {r.text[:150]}")
                continue
            body = r.json()
            msg = body["assistant_message"]
            used = set(msg["metadata"].get("tools_used", []))
            switched = (body.get("switched_to_source") or {}).get("name")
            now = owner.get(f"/cameras/{made['rtsp']}/status").json().get("live_ai") or {}
            check(f"9/10. assistant ({lang}): {q}", bool(used & tools) or bool(msg["metadata"].get("route")),
                  f"[{dt:.1f} s, switched to {switched}, tools {sorted(used)}] {msg['content'][:220]} || live AI now: {now.get('counts')}", seconds=dt, answer=msg["content"])

    if want("upgrade") and "onvif" in made:  # camera platform upgrade: capabilities, PTZ, camera events, diagnostics
        cid = made["onvif"]
        caps = owner.get(f"/cameras/{cid}/capabilities").json()
        check("capability profile (ONVIF camera)", caps.get("ptz") is True and caps.get("events") is True and caps.get("talk") is False and caps.get("substream") is True,
              json.dumps({k: caps[k] for k in ("video", "audio", "talk", "ptz", "recording", "events", "h264", "h265", "substream")}))
        st = owner.get(f"/cameras/{cid}/streams").json()
        check("stream usage (main / sub / AI / recording)", st["usage"]["recording"] == "main" and st["usage"]["mobile"] == "sub", json.dumps(st["usage"]))
        info = owner.get(f"/cameras/{cid}/ptz").json()
        before = info.get("position", {})
        owner.post(f"/cameras/{cid}/ptz", json={"action": "move", "pan": 0.8, "tilt": 0.0, "zoom": 0.0})
        time.sleep(1.0)
        owner.post(f"/cameras/{cid}/ptz", json={"action": "stop"})
        after = owner.get(f"/cameras/{cid}/ptz").json().get("position", {})
        check("PTZ continuous move + stop", (after.get("pan") or 0) > (before.get("pan") or 0) + 0.2, f"pan {before.get('pan')} -> {after.get('pan')}; presets {[x['name'] for x in info.get('presets', [])]}")
        owner.post(f"/cameras/{cid}/ptz", json={"action": "preset", "preset": "2"})
        pos = owner.get(f"/cameras/{cid}/ptz").json().get("position", {})
        check("PTZ go to preset", abs((pos.get("pan") or 0) - 0.6) < 0.01, f"position {pos}")
        r = viewer.post(f"/cameras/{cid}/ptz", json={"action": "stop"})
        check("PTZ refused without the ptz permission", r.status_code in (403, 404), f"status {r.status_code}")
        t0 = time.time()
        motion = []
        while time.time() - t0 < 45 and not motion:
            motion = [e for e in owner.get(f"/cameras/{cid}/events", params={"minutes": 5}).json() if e["type"] == "camera_motion"]
            time.sleep(2)
        check("ONVIF camera events (pull point) stored", bool(motion) and motion[0]["evidence"] == "device", f"{motion[0]['description'] if motion else 'none'} after {time.time() - t0:.0f} s")
        d = owner.get(f"/cameras/{cid}/diagnostics").json()
        names = {c["name"]: c["ok"] for c in d.get("checks", [])}
        check("diagnostics on a connected camera", d.get("ok") and names.get("ai_processing") and names.get("media_server") and names.get("camera_events"),
              ", ".join(f"{k}={'ok' if v else ('-' if v is None else 'FAIL')}" for k, v in names.items()))
        found = owner.post("/cameras/discover", json={"timeout": 3}).json()["devices"]
        cam_dev = next((x for x in found if "2143" in (x.get("name") or "")), {})
        check("discovery details (profiles, already added)", "T" in cam_dev.get("profiles", []) and cam_dev.get("already_added") is True,
              f"{cam_dev.get('name')} profiles={cam_dev.get('profiles')} already_added={cam_dev.get('already_added')}")
        conns = {c["id"]: c["status"] for c in owner.get("/cameras-connectors").json()}
        check("connector list is honest", conns.get("imou") in ("needs_configuration", "untested") and conns.get("hikvision") == "via_standard", json.dumps(conns))
        body = owner.get(f"/cameras/{cid}").json()
        check("device service addresses not exposed", "services" not in body["device"] and "xaddr" not in body["device"], json.dumps(body["device"]))
    if want("upgrade") and "rtsp" in made:
        ev = [e for e in owner.get(f"/cameras/{made['rtsp']}/events", params={"minutes": 20}).json() if e["track_id"] is not None]
        with_box = [e for e in ev if e.get("bbox") and e.get("clip")]
        check("event memory: bounding box + clip reference", bool(with_box), f"{len(with_box)}/{len(ev)} events; e.g. {with_box[0]['type']} bbox={with_box[0]['bbox']}" if with_box else "none")
        if with_box:
            clip = with_box[-1]["clip"]
            r = owner.get(f"/cameras/{made['rtsp']}/playback", params={"start": clip["start"], "duration": clip["duration"]})
            check("event opens its recording", r.status_code == 200 and len(r.content) > 10000, f"{len(r.content) // 1024} KB")
        ov = owner.get("/cameras-overview").json()
        check("batched GPU inference across cameras", any(v["mean_batch"] > 1 for v in ov.get("batching", {}).values()), json.dumps(ov.get("batching")))

    if want("assistant") and "wildlife" in made and "rtsp" in made:
        qs = [("en", "Are there any animals on Waterhole?", "live_animals"), ("en", "When did the zebra arrive on Waterhole?", "live_arrival"),
              ("en", "How many people are there on Camera 3?", "live_count"),
              ("rw", "Ni izihe nyamaswa ziri kuri Waterhole?", "live_animals"), ("rw", "Hari abantu bangahe kuri Camera 3?", "live_count"),
              ("rw", "Ni iki cyabaye kuri Camera 3 mu minota icumi ishize?", "live_period"), ("rw", "Ni iki kiri kuri camera yo ku irembo?", "live_now")]
        for lang, q, route in qs:
            conv = owner.post("/conversations", json={"video_source_id": made.get("onvif") or made["rtsp"], "language": lang}).json()
            t0 = time.time()
            r = owner.post(f"/conversations/{conv['id']}/messages", json={"question": q}, timeout=300)
            if r.status_code != 200:
                check(f"assistant ({lang}): {q}", False, f"{r.status_code} {r.text[:150]}")
                continue
            m = r.json()["assistant_message"]
            got = m["metadata"].get("route")
            check(f"assistant ({lang}): {q}", got == route, f"[{time.time() - t0:.1f} s, route {got}, en: {m['metadata'].get('answer_en', '')[:120]}] {m['content'][:200]}", answer=m["content"])

    if want("reconnect") and "rtsp" in made:  # acceptance 6
        path = "Streaming/Channels/101"
        from subprocess import run

        run([sys.executable, str(Path(__file__).resolve().parents[1] / "tools/camsim/camsim.py"), "--kill", path], check=False)
        t0 = time.time()
        down = False
        while time.time() - t0 < 90:
            s = owner.get(f"/cameras/{made['rtsp']}").json()
            if s["state"] != "online":
                down = True
                break
            time.sleep(1)
        check("6. connection loss detected", down, f"state={s['state']} diagnosis={s['health'].get('diagnosis')} after {time.time() - t0:.1f} s")
        run([sys.executable, str(Path(__file__).resolve().parents[1] / "tools/camsim/camsim.py"), "--restart", path], check=False)
        t1 = time.time()
        back = False
        while time.time() - t1 < 180:
            s = owner.get(f"/cameras/{made['rtsp']}").json()
            if s["state"] == "online":
                back = True
                break
            time.sleep(1)
        check("6. auto-reconnect", back, f"back online after {time.time() - t1:.1f} s, reconnects={s['health'].get('reconnects')}", seconds=time.time() - t1)

    if want("gateway"):  # acceptance 5: remote camera through an outbound-only gateway
        import subprocess

        backend = Path(__file__).resolve().parents[1]
        state = backend / "gateway_agent/gateway-acceptance.json"
        g = owner.post("/gateways", json={"name": "Farm gateway", "location": "nyagatare"}).json()
        check("5. gateway created with one-time enrolment code", g.get("enrollment_token", "").startswith("vge_"), f"expires {g.get('expires_at')}")
        agent = [sys.executable, "-m", "gateway_agent.visionary_gateway", "--state", str(state)]
        enr = subprocess.run([*agent, "enroll", "--hub", "http://127.0.0.1:8000", "--token", g["enrollment_token"]], cwd=backend, capture_output=True, text=True)
        check("5. gateway enrolled", enr.returncode == 0, (enr.stdout or enr.stderr).strip()[-120:])
        again = subprocess.run([*agent, "enroll", "--hub", "http://127.0.0.1:8000", "--token", g["enrollment_token"]], cwd=backend, capture_output=True, text=True,
                               env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        check("5. enrolment code works only once", again.returncode != 0, (again.stderr or again.stdout).strip()[-100:])
        proc = subprocess.Popen([*agent, "run"], cwd=backend, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            t0 = time.time()
            while time.time() - t0 < 30 and owner.get(f"/gateways/{g['id']}").json()["status"] != "online":
                time.sleep(0.5)
            check("5. gateway online (outbound WebSocket)", owner.get(f"/gateways/{g['id']}").json()["status"] == "online", f"after {time.time() - t0:.1f} s")
            t0 = time.time()
            r = owner.post("/cameras/test-connection", json={"connection": {"type": "rtsp", "url": f"{SIM}/nvr/ch2"}, "gateway_id": g["id"]}).json()
            check("5. camera tested by the gateway on its LAN", r.get("diagnosis") == "OK", f"{r.get('diagnosis')} in {time.time() - t0:.1f} s")
            r = owner.post("/cameras", json={"name": "Barn (via gateway)", "connection": {"type": "rtsp", "url": f"{SIM}/nvr/ch2"}, "gateway_id": g["id"]})
            if check("5. add camera via gateway", r.status_code == 201, r.text[:120]):
                cam = r.json()[0]
                made["gateway"] = cam["id"]
                t0 = time.time()
                online = False
                while time.time() - t0 < 60 and not online:
                    online = owner.get(f"/cameras/{cam['id']}").json()["state"] == "online"
                    time.sleep(1)
                with httpx.Client(timeout=10, trust_env=False) as raw:
                    p = raw.get(f"http://127.0.0.1:9997/v3/paths/get/gw/{uuid.UUID(g['id']).hex}/{uuid.UUID(cam['id']).hex}/main").json()
                src = (p.get("source") or {}).get("type")
                check("5. gateway pushes the stream (hub never connects to the camera)", online and src in ("rtspSession", "rtspsSession"),
                      f"online after {time.time() - t0:.1f} s, media source = {src}")
                ok, msg, _ = whep(owner, cam["id"])
                check("5. live view of a gateway camera", ok, msg)
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)  # power loss: gateway + its pushes
                proc.wait(10)
                t0 = time.time()
                state_now = ""
                while time.time() - t0 < 90:
                    state_now = owner.get(f"/cameras/{cam['id']}").json()["state"]
                    if state_now == "gateway_offline":
                        break
                    time.sleep(1)
                check("5. gateway loss diagnosed", state_now == "gateway_offline", f"{state_now} after {time.time() - t0:.1f} s")
                proc = subprocess.Popen([*agent, "run"], cwd=backend, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                t0 = time.time()
                while time.time() - t0 < 120:
                    state_now = owner.get(f"/cameras/{cam['id']}").json()["state"]
                    if state_now == "online":
                        break
                    time.sleep(1)
                check("5. gateway reconnect restores the stream", state_now == "online", f"online again after {time.time() - t0:.1f} s")
            owner.delete(f"/gateways/{g['id']}")
            time.sleep(2)
            check("5. revoked gateway is disconnected", proc.poll() is not None or owner.get("/gateways").json() == [] , "revoked")
        finally:
            proc.terminate()
            state.unlink(missing_ok=True)

    if want("security"):  # acceptance 12
        blob = json.dumps(owner.get("/cameras").json()) + json.dumps(owner.get("/video-sources").json())
        leaks = [x for x in (PASSWORD, "Cam%232026", "admin:") if x in blob]
        check("12. no password in camera/video APIs", not leaks, f"leaks={leaks}")
        logs = ""
        for p in (Path(__file__).resolve().parents[1] / "data/mediamtx.log",):
            logs += p.read_text(errors="ignore") if p.exists() else ""
        bk = Path(sys.argv[0]).parent  # backend log is passed via env if available
        import os

        if os.environ.get("BACKEND_LOG") and Path(os.environ["BACKEND_LOG"]).exists():
            logs += Path(os.environ["BACKEND_LOG"]).read_text(errors="ignore")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.cameras.media_server import internal_password

        check("12. no password in logs", PASSWORD not in logs and "Cam%232026" not in logs, f"scanned {len(logs)} chars")
        check("12. no internal media secret in logs", internal_password() not in logs, "media server + backend logs")
        del bk
        cfg = (Path(__file__).resolve().parents[1] / "data/mediamtx.yml").read_text()
        check("12. media server config has no camera passwords", PASSWORD not in cfg and "Cam%23" not in cfg, "paths are added at runtime via the local API")
        r = httpx.post(f"{API}/media/auth", json={"action": "read", "path": "cam/x/main"}, headers={"X-Forwarded-For": "8.8.8.8"}, trust_env=False)
        check("media auth hook rejects anonymous / non-local calls", r.status_code in (401, 403), f"status {r.status_code}")
        dbfile = Path(__file__).resolve().parents[1] / "dev.db"
        raw_db = dbfile.read_bytes()
        check("12. password encrypted at rest", PASSWORD.encode() not in raw_db and b"Cam%232026" not in raw_db, "dev.db scanned")
        _ = re

    out = Path(__file__).resolve().parents[2] / "bench/camera_acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "cameras": made, "results": results}, indent=2))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed -> {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
