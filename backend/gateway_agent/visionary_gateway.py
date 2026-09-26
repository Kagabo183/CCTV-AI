"""Visionary Gateway agent: runs on the customer's network, next to the cameras.

    python -m gateway_agent.visionary_gateway enroll --hub https://visionary.example --token vge_...   # once
    python -m gateway_agent.visionary_gateway run                                                     # as a service

Only OUTBOUND connections: HTTPS for enrolment, a WebSocket for control, RTSP(S) to push video. Nothing listens
on the customer network except WS-Discovery replies to the gateway's own probe; no router port forwarding.

Commands from the hub:
  probe          test a camera on this LAN (same checks and diagnosis codes as the cloud)
  discover       ONVIF WS-Discovery on this gateway's LAN interfaces (--networks limits them)
  start_streams  pull each camera stream and push it unchanged (ffmpeg -c copy) to the hub's media server
  stop_stream    stop one camera's pushes

The identity (gateway id + secret) is kept in gateway.json next to this file (or --state), readable only by
the gateway's user. Camera passwords are held in memory only, for as long as a stream runs.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the shared camera modules (app/cameras)

import httpx  # noqa: E402
import websockets  # noqa: E402

from app.cameras.proc import bind_children_to_this_process, preexec  # noqa: E402

VERSION = "0.1.0"
log = logging.getLogger("visionary-gateway")


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


class Pusher:
    """One camera stream: pull from the camera on the LAN, push to the hub. Restarts with backoff."""

    def __init__(self, source: str, target: str) -> None:
        self.source, self.target = source, target
        self.proc: subprocess.Popen[bytes] | None = None
        self.restarts = 0
        self.next_start = 0.0
        self.started_at = 0.0
        self.stopped = False

    def tick(self) -> None:
        if self.stopped:
            return
        if self.proc is not None and self.proc.poll() is None:
            if time.time() - self.started_at > 60:
                self.restarts = 0  # stable again
            return
        if time.time() < self.next_start:
            return
        if self.proc is not None:
            self.restarts += 1
        self.next_start = time.time() + min(60, 2 ** min(self.restarts, 6))
        # -loglevel quiet: both URLs carry credentials and must never reach a log
        self.proc = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-loglevel", "quiet", "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", self.source,
                                      "-c", "copy", "-an", "-f", "rtsp", "-rtsp_transport", "tcp", self.target],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     preexec_fn=preexec())
        self.started_at = time.time()

    def stop(self) -> None:
        self.stopped = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Gateway:
    def __init__(self, state: dict[str, Any], networks: list[str]) -> None:
        self.state = state
        self.networks = [ipaddress.ip_network(n, strict=False) for n in networks]
        self.pushers: dict[str, Pusher] = {}  # "<camera>/<role>" -> pusher
        self.started = time.time()

    # ------------------------------------------------------------------ commands
    async def probe(self, args: dict[str, Any]) -> dict[str, Any]:
        from app.cameras.connections import build_connection

        spec = args["spec"]
        host = urlsplit(spec.get("url") or ("http://" + spec.get("xaddr", "") if "://" not in spec.get("xaddr", "") else spec["xaddr"])).hostname or ""
        self._check_lan(host)
        result = await build_connection(spec, args.get("username"), args.get("password")).probe(measure=True)
        return result.as_dict()

    async def discover(self, args: dict[str, Any]) -> dict[str, Any]:
        from app.cameras import onvif

        interfaces = [ip for ip in onvif.local_ipv4() if self._lan_allowed(ip)]
        found = await asyncio.to_thread(onvif.discover, float(args.get("timeout", 3)), interfaces)
        return {"devices": [{"name": d.name, "ip": d.ip, "xaddrs": d.xaddrs, "id": d.address, "scopes": d.scopes} for d in found if self._lan_allowed(d.ip)], "networks": interfaces}

    async def start_streams(self, args: dict[str, Any]) -> dict[str, Any]:
        hub = urlsplit(self.state["hub"])
        media = self.state["media"]
        user, secret = media["user"], self.state["secret"]
        scheme = "rtsps" if media.get("tls") else "rtsp"
        port = media.get("rtsps_port" if media.get("tls") else "rtsp_port", 8554)
        started = []
        for s in args.get("streams", []):
            if not s["path"].startswith(media["path_prefix"]):
                continue  # the hub may only direct pushes into this gateway's own namespace
            key = f"{s['camera_id']}/{s['role']}"
            target = f"{scheme}://{user}:{secret}@{hub.hostname}:{port}/{s['path']}"
            old = self.pushers.get(key)
            if old is not None and old.source == s["source"] and old.target == target and not old.stopped:
                continue
            if old is not None:
                old.stop()
            self.pushers[key] = Pusher(s["source"], target)
            self.pushers[key].tick()
            started.append(key)
        return {"started": started}

    async def stop_stream(self, args: dict[str, Any]) -> dict[str, Any]:
        stopped = [k for k in list(self.pushers) if k.startswith(args["camera_id"] + "/")]
        for k in stopped:
            self.pushers.pop(k).stop()
        return {"stopped": stopped}

    def _lan_allowed(self, ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        if self.networks:
            return any(addr in n for n in self.networks)
        return addr.is_private or addr.is_loopback

    def _check_lan(self, host: str) -> None:
        import socket

        try:
            ips = {i[4][0] for i in socket.getaddrinfo(host, None)}
        except socket.gaierror:
            return  # the probe reports DNS_FAILED
        if not all(self._lan_allowed(ip) for ip in ips):
            raise ValueError("This gateway only connects to cameras on its own local network.")

    def health(self) -> dict[str, Any]:
        return {"uptime": round(time.time() - self.started), "version": VERSION,
                "streams": {k: {"running": p.running, "restarts": p.restarts} for k, p in self.pushers.items()}}

    # ------------------------------------------------------------------ control loop
    async def run(self) -> None:
        hub = self.state["hub"].rstrip("/")
        ws_url = hub.replace("https://", "wss://").replace("http://", "ws://") + self.state["control_path"]
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(ws_url, additional_headers={"Authorization": f"Bearer {self.state['secret']}"}, ping_interval=20, open_timeout=15) as ws:
                    log.info("Connected to %s", hub)
                    backoff = 1.0
                    await self._session(ws)
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                rejected = getattr(getattr(exc, "response", None), "status_code", None) == 403  # refused before the handshake
                if rejected or getattr(getattr(exc, "rcvd", None), "code", None) == 4401:
                    log.error("The hub rejected this gateway (revoked?). Enrol again.")
                    return
                log.warning("Hub connection lost (%s); retrying in %.0f s", type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def _session(self, ws: Any) -> None:
        heartbeat = int(self.state.get("heartbeat_seconds", 20))

        async def beats() -> None:
            while True:
                for p in self.pushers.values():
                    p.tick()  # restarts pushes that died (camera reboot, network blip)
                await ws.send(json.dumps({"op": "heartbeat", "args": self.health()}))
                await asyncio.sleep(min(5, heartbeat))

        task = asyncio.create_task(beats())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == "hello":
                    self.state["heartbeat_seconds"] = msg["args"].get("heartbeat_seconds", heartbeat)
                    continue
                asyncio.create_task(self._handle(ws, msg))
        finally:
            task.cancel()

    async def _handle(self, ws: Any, msg: dict[str, Any]) -> None:
        handlers = {"probe": self.probe, "discover": self.discover, "start_streams": self.start_streams, "stop_stream": self.stop_stream}
        reply: dict[str, Any] = {"reply_to": msg.get("id")}
        try:
            handler = handlers.get(msg.get("op", ""))
            if handler is None:
                raise ValueError(f"unknown command {msg.get('op')!r}")
            reply["result"] = await handler(msg.get("args") or {})
        except Exception as exc:  # noqa: BLE001 - the hub shows the message; never include credentials
            from app.cameras.credentials import redact

            reply["error"] = redact(str(exc))[:300]
        await ws.send(json.dumps(reply))

    def shutdown(self) -> None:
        for p in self.pushers.values():
            p.stop()


def _local_networks() -> list[str]:
    from app.cameras import onvif

    return [str(ipaddress.ip_network(f"{ip}/24", strict=False)) for ip in onvif.local_ipv4()]


def enroll(hub: str, token: str, state_file: Path) -> None:
    body = {"token": token, "hostname": platform.node(), "version": VERSION, "networks": _local_networks()}
    r = httpx.post(hub.rstrip("/") + "/api/gateways/enroll", json=body, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"Enrolment failed: {r.json().get('detail', r.text)}")
    d = r.json()
    state = {"hub": hub, "gateway_id": d["gateway_id"], "secret": d["secret"], "control_path": d["control_path"], "media": d["media"]}
    state_file.write_text(json.dumps(state, indent=2))
    try:
        os.chmod(state_file, 0o600)
    except OSError:
        pass
    print(f"Enrolled as gateway {d['gateway_id']}. Start it with: run")


def main() -> None:
    ap = argparse.ArgumentParser(description="Visionary Gateway")
    ap.add_argument("--state", type=Path, default=Path(__file__).with_name("gateway.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll")
    e.add_argument("--hub", required=True)
    e.add_argument("--token", required=True)
    r = sub.add_parser("run")
    r.add_argument("--networks", default="", help="comma-separated LAN networks cameras may be on (default: private networks)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "enroll":
        enroll(a.hub, a.token, a.state)
        return
    state = json.loads(a.state.read_text())
    bind_children_to_this_process()  # pushes die with the gateway: a crashed gateway leaves no orphan streams
    gw = Gateway(state, [n for n in a.networks.split(",") if n])
    signal.signal(signal.SIGTERM, lambda *_: (gw.shutdown(), sys.exit(0)))
    try:
        asyncio.run(gw.run())
    except KeyboardInterrupt:
        pass
    finally:
        gw.shutdown()


if __name__ == "__main__":
    main()
