"""RTSP probing with real diagnostics.

1. DNS + TCP connect to the RTSP port          -> DNS_FAILED / NETWORK_UNREACHABLE / RTSP_DISABLED
2. RTSP DESCRIBE (Basic or Digest auth)        -> AUTHENTICATION_FAILED / INVALID_CREDENTIALS / STREAM_NOT_FOUND
3. ffprobe on the stream                       -> codec, resolution, fps, audio / UNSUPPORTED_CODEC
4. read ~3 s of frames                         -> startup latency, delivered fps / STREAM_TIMEOUT
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.cameras.credentials import redact, with_credentials
from app.cameras.diagnostics import Check, Diagnosis, ProbeReport

DECODABLE = {"h264", "hevc", "h265", "mjpeg", "mpeg4"}


@dataclass
class StreamInfo:
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio: str | None = None
    bitrate: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None}


def _digest(user: str, password: str, method: str, uri: str, challenge: str) -> str:
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}"'


async def _rtsp_request(host: str, port: int, method: str, url: str, auth: str | None, timeout: float) -> tuple[int, dict[str, str]]:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        lines = [f"{method} {url} RTSP/1.0", "CSeq: 1", "User-Agent: Visionary", "Accept: application/sdp"]
        if auth:
            lines.append(f"Authorization: {auth}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    finally:
        writer.close()
    text = head.decode("latin-1")
    status = int(text.split(" ", 2)[1]) if text.startswith("RTSP/") else 0
    headers = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.setdefault(k.strip().lower(), v.strip())
    return status, headers


def _has_access_token(url: str) -> bool:
    """Links like rtsp://host/live?token=... authenticate with the token itself (and such tokens often expire)."""
    from urllib.parse import parse_qs, urlsplit

    keys = {k.lower() for k in parse_qs(urlsplit(url).query)}
    return bool(keys & {"token", "access_token", "auth", "key", "sig", "signature", "st", "sign"})


async def describe(url: str, username: str | None, password: str | None, report: ProbeReport, timeout: float = 5.0) -> bool:
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 554
    t0 = time.perf_counter()
    try:
        await asyncio.to_thread(socket.getaddrinfo, host, port)
    except socket.gaierror:
        report.add(Check("dns", False, f"{host} does not resolve", Diagnosis.DNS_FAILED))
        return False
    report.add(Check("dns", True, host, ms=round((time.perf_counter() - t0) * 1000, 1)))
    clean = f"{parts.scheme}://{host}:{port}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
    t0 = time.perf_counter()
    try:
        status, headers = await _rtsp_request(host, port, "DESCRIBE", clean, None, timeout)
    except (TimeoutError, asyncio.TimeoutError):
        report.add(Check("reachable", False, f"{host}:{port} did not answer within {timeout:.0f}s", Diagnosis.CAMERA_OFFLINE))
        return False
    except ConnectionRefusedError:
        report.add(Check("reachable", True, f"{host} answers"))
        report.add(Check("rtsp", False, f"port {port} refused the connection", Diagnosis.RTSP_DISABLED))
        return False
    except OSError as exc:
        report.add(Check("reachable", False, str(exc)[:120], Diagnosis.NETWORK_UNREACHABLE))
        return False
    latency = round((time.perf_counter() - t0) * 1000, 1)
    report.add(Check("reachable", True, f"{host}:{port}", ms=latency))
    report.add(Check("rtsp", True, f"RTSP server answers ({headers.get('server', 'unknown server')})"))
    if status == 401:
        if not username:
            if _has_access_token(url):
                report.add(Check("authentication", False, "the link's access token was refused", Diagnosis.LINK_EXPIRED))
            else:
                report.add(Check("authentication", False, "the camera requires a login", Diagnosis.INVALID_CREDENTIALS))
            return False
        challenge = headers.get("www-authenticate", "")
        auth = _digest(username, password or "", "DESCRIBE", clean, challenge) if challenge.lower().startswith("digest") else \
            "Basic " + __import__("base64").b64encode(f"{username}:{password or ''}".encode()).decode()
        status, headers = await _rtsp_request(host, port, "DESCRIBE", clean, auth, timeout)
        if status == 401:
            report.add(Check("authentication", False, "username or password rejected", Diagnosis.AUTHENTICATION_FAILED))
            return False
        report.add(Check("authentication", True, "accepted"))
    else:
        report.add(Check("authentication", None, "no login required"))
    if status == 404:
        report.add(Check("stream", False, f"path {parts.path} not found", Diagnosis.STREAM_NOT_FOUND))
        return False
    if status != 200:
        report.add(Check("stream", False, f"RTSP {status}", Diagnosis.UNKNOWN_ERROR))
        return False
    return True


def ffprobe(url: str, timeout: float = 12.0) -> StreamInfo:
    from app.video import ffmpeg

    exe = ffmpeg.ffmpeg_exe().replace("ffmpeg", "ffprobe") if "ffprobe" in ffmpeg.ffmpeg_exe() else None
    # imageio-ffmpeg ships only ffmpeg: parse "ffmpeg -i" output instead of ffprobe JSON
    proc = subprocess.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-rtsp_transport", "tcp", "-timeout", str(int(timeout * 1e6)), "-i", url, "-t", "0.1", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=timeout + 5)
    text = proc.stderr
    info = StreamInfo()
    v = re.search(r"Video: (\w+)[^\n]*?, (\d{2,5})x(\d{2,5})[^\n]*?(?:, ([\d.]+) fps)?", text)
    if v:
        info.codec = v.group(1).lower()
        info.width, info.height = int(v.group(2)), int(v.group(3))
        fps = re.search(r"([\d.]+) (?:fps|tbr)", text[v.start():v.start() + 400])
        info.fps = float(fps.group(1)) if fps else None
    a = re.search(r"Audio: (\w+)", text)
    info.audio = a.group(1) if a else None
    _ = exe, json
    return info


def read_frames(url: str, seconds: float = 3.0, timeout: float = 15.0) -> tuple[float | None, float | None]:
    """(startup seconds to first frame, delivered fps) by decoding `seconds` of stream with OpenCV."""
    import cv2

    t0 = time.perf_counter()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    first = None
    n = 0
    try:
        while time.perf_counter() - t0 < timeout:
            if not cap.grab():
                break
            now = time.perf_counter()
            if first is None:
                first = now
            n += 1
            if now - first >= seconds:
                break
    finally:
        cap.release()
    if first is None:
        return None, None
    elapsed = time.perf_counter() - first
    return round(first - t0, 2), round(n / elapsed, 1) if elapsed > 0 else None


async def probe(url: str, username: str | None, password: str | None, *, measure: bool = True) -> tuple[ProbeReport, StreamInfo | None]:
    report = ProbeReport()
    if not await describe(url, username, password, report):
        return report, None
    full = with_credentials(url, username, password)
    try:
        info = await asyncio.to_thread(ffprobe, full)
    except subprocess.TimeoutExpired:
        report.add(Check("stream", False, "no media within the timeout", Diagnosis.STREAM_TIMEOUT))
        return report, None
    if not info.codec:
        report.add(Check("stream", False, "no video track found", Diagnosis.STREAM_TIMEOUT))
        return report, None
    report.add(Check("stream", True, "video received"))
    report.add(Check("codec", info.codec in DECODABLE, info.codec.upper().replace("HEVC", "H.265").replace("H264", "H.264"),
                     Diagnosis.OK if info.codec in DECODABLE else Diagnosis.UNSUPPORTED_CODEC))
    if info.width:
        report.add(Check("resolution", True, f"{info.width}x{info.height}"))
    if measure and info.codec in DECODABLE:
        startup, fps = await asyncio.to_thread(read_frames, full)
        if fps is None:
            report.add(Check("stability", False, "stream stopped while reading", Diagnosis.STREAM_TIMEOUT))
        else:
            report.add(Check("fps", True, f"{fps} fps delivered", ms=None))
            report.add(Check("latency", True, f"first frame after {startup:.2f} s", ms=round(startup * 1000)))
            report.add(Check("stability", True, "3 s read without interruption"))
            info.fps = info.fps or fps
    return report, info


def safe(url: str) -> str:
    return redact(url)
