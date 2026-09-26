"""CameraConnection: one interface for every way of reaching a camera.

    CameraConnection
        ├── RTSPConnection      rtsp://host/path (+ optional sub-stream URL)
        ├── ONVIFConnection     ONVIF device service -> profiles -> stream URIs (Profile T/Media2 first)
        ├── NVRConnection       ONVIF NVR: each video source is a channel (becomes its own camera)
        ├── HLSConnection       http(s) HLS / progressive streams
        ├── GatewayConnection   any of the above, executed by a Visionary Gateway on the camera's LAN
        └── VendorConnection    manufacturer cloud APIs (app/cameras/vendors), official APIs only

probe() returns a ProbeResult: diagnosis checks, a capability record and channels with their streams
(main / sub / low). Stream URIs never contain credentials; those stay in the credential vault.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from app.cameras import onvif, rtsp
from app.cameras.credentials import redact, split_credentials, with_credentials
from app.cameras.diagnostics import Check, Diagnosis, ProbeReport


@dataclass
class StreamProfile:
    role: str  # main | sub | low
    uri: str  # without credentials
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio: bool = False
    token: str | None = None  # ONVIF profile token

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False) or k == "audio"}


@dataclass
class ChannelInfo:
    key: str
    name: str
    streams: list[StreamProfile] = field(default_factory=list)

    def stream(self, role: str) -> StreamProfile | None:
        return next((s for s in self.streams if s.role == role), None) or (self.streams[0] if self.streams else None)


@dataclass
class ProbeResult:
    report: ProbeReport
    capabilities: dict[str, Any] = field(default_factory=dict)
    channels: list[ChannelInfo] = field(default_factory=list)
    device: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.report.ok and bool(self.channels)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProbeResult:
        """Rebuild a probe made elsewhere (by a gateway on the camera's network)."""
        report = ProbeReport([Check(c["name"], c.get("ok"), c.get("detail", ""), Diagnosis(c.get("code", "OK")), c.get("ms")) for c in d.get("checks", [])])
        channels = [ChannelInfo(c["key"], c["name"], [StreamProfile(**{k: v for k, v in st.items() if k in StreamProfile.__dataclass_fields__}) for st in c.get("streams", [])])
                    for c in d.get("channels", [])]
        return cls(report, d.get("capabilities") or {}, channels, d.get("device") or {})

    def as_dict(self) -> dict[str, Any]:
        return {**self.report.as_dict(), "capabilities": self.capabilities, "device": self.device,
                "channels": [{"key": c.key, "name": c.name, "streams": [s.as_dict() for s in c.streams]} for c in self.channels]}


def assign_roles(profiles: list[StreamProfile]) -> list[StreamProfile]:
    """Largest resolution = main, next = sub, smallest (if a third) = low."""
    ordered = sorted(profiles, key=lambda p: -((p.width or 0) * (p.height or 0)))
    for i, p in enumerate(ordered):
        p.role = "main" if i == 0 else "sub" if i == 1 else "low"
    return ordered


def codec_name(codec: str | None) -> str | None:
    if not codec:
        return None
    c = codec.lower()
    return {"hevc": "H.265", "h265": "H.265", "h264": "H.264", "mjpeg": "MJPEG", "jpeg": "MJPEG"}.get(c, codec.upper())


class CameraConnection(ABC):
    kind: str

    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        self.username, self.password = username, password

    @abstractmethod
    async def probe(self, *, measure: bool = True) -> ProbeResult: ...

    def spec(self) -> dict[str, Any]:
        """How to reconnect later (stored in source_metadata; no secrets)."""
        return {"type": self.kind}


class RTSPConnection(CameraConnection):
    kind = "rtsp"

    def __init__(self, url: str, username: str | None = None, password: str | None = None, sub_url: str | None = None) -> None:
        clean, u, p = split_credentials(url)
        super().__init__(username or u, password if password is not None else p)
        self.url = clean
        self.sub_url = split_credentials(sub_url)[0] if sub_url else None

    async def probe(self, *, measure: bool = True) -> ProbeResult:
        report, info = await rtsp.probe(self.url, self.username, self.password, measure=measure)
        result = ProbeResult(report)
        if info is None:
            return result
        streams = [StreamProfile("main", self.url, codec_name(info.codec), info.width, info.height, info.fps, bool(info.audio))]
        if self.sub_url:
            sub_report, sub = await rtsp.probe(self.sub_url, self.username, self.password, measure=False)
            if sub is not None:
                streams.append(StreamProfile("sub", self.sub_url, codec_name(sub.codec), sub.width, sub.height, sub.fps, bool(sub.audio)))
                report.add(Check("substream", True, f"{sub.width}x{sub.height} {codec_name(sub.codec)}"))
            else:
                failure = sub_report.failure
                report.add(Check("substream", None, f"sub stream not usable ({failure.code.value if failure else 'unknown'})"))
        result.channels = [ChannelInfo("1", "Camera", assign_roles(streams))]
        result.capabilities = {
            "connection_type": "rtsp", "video": True, "audio": bool(info.audio),
            "h264": any(s.codec == "H.264" for s in streams), "h265": any(s.codec == "H.265" for s in streams),
            "mainstream": True, "substream": len(streams) > 1,
            # RTSP alone cannot tell these; ONVIF or a vendor integration can
            "ptz": None, "events": None, "recording": None, "two_way_audio": None,
        }
        return result

    def spec(self) -> dict[str, Any]:
        return {"type": "rtsp", "url": self.url, "sub_url": self.sub_url}


class ONVIFConnection(CameraConnection):
    kind = "onvif"

    def __init__(self, xaddr: str, username: str | None = None, password: str | None = None) -> None:
        super().__init__(username, password)
        self.xaddr = onvif.device_url(xaddr)

    async def probe(self, *, measure: bool = True) -> ProbeResult:
        report, dev = await onvif.probe(self.xaddr, self.username, self.password)
        result = ProbeResult(report)
        if dev is None:
            return result
        result.capabilities = dev.capabilities()
        result.device = {"manufacturer": dev.manufacturer, "model": dev.model, "firmware": dev.firmware, "serial": dev.serial,
                         "onvif_profiles": dev.profiles_supported, "media_version": dev.media_version, "xaddr": self.xaddr,
                         # service addresses for PTZ / events later (server-side only; not returned by the camera API)
                         "services": {k: dev.services[ns] for k, ns in (("ptz", onvif.PTZ_NS), ("events", onvif.EVENTS_NS), ("recording", onvif.RECORDING_NS)) if ns in dev.services}}
        for i, (source, profiles) in enumerate(dev.channels.items(), start=1):
            streams = assign_roles([StreamProfile("main", p.stream_uri or "", codec_name(p.encoding), p.width, p.height, p.fps, p.audio, p.token)
                                    for p in profiles if p.stream_uri])
            name = profiles[0].name if len(dev.channels) > 1 else (dev.model or "Camera")
            result.channels.append(ChannelInfo(source, name, streams))
        # The stream itself must also accept the credentials (some cameras use separate RTSP users)
        first = result.channels[0].stream("main") if result.channels else None
        if first is not None:
            stream_report, info = await rtsp.probe(first.uri, self.username, self.password, measure=measure)
            for c in stream_report.checks:
                if c.name not in ("dns", "reachable", "authentication"):
                    report.add(c)
                elif c.ok is False:
                    report.add(Check("rtsp_" + c.name, False, c.detail, c.code))
        return result

    def spec(self) -> dict[str, Any]:
        return {"type": "onvif", "xaddr": self.xaddr}


class NVRConnection(ONVIFConnection):
    """An ONVIF NVR/DVR. Each channel (video source) is registered as its own camera under the NVR."""

    kind = "nvr"

    async def probe(self, *, measure: bool = False) -> ProbeResult:
        result = await super().probe(measure=measure)
        if result.channels:
            result.report.add(Check("channels", True, f"{len(result.channels)} channel(s): " + ", ".join(c.name for c in result.channels)))
        result.capabilities["connection_type"] = "nvr"
        return result

    def spec(self) -> dict[str, Any]:
        return {"type": "nvr", "xaddr": self.xaddr}


class HLSConnection(CameraConnection):
    kind = "hls"

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    async def probe(self, *, measure: bool = True) -> ProbeResult:
        report = ProbeReport()
        try:
            info = await asyncio.to_thread(rtsp.ffprobe, self.url)
        except Exception as exc:  # noqa: BLE001
            report.add(Check("stream", False, redact(str(exc))[:160], Diagnosis.STREAM_TIMEOUT))
            return ProbeResult(report)
        if not info.codec:
            report.add(Check("stream", False, "no video found at this address", Diagnosis.STREAM_NOT_FOUND))
            return ProbeResult(report)
        report.add(Check("stream", True, "video received"))
        report.add(Check("codec", info.codec in rtsp.DECODABLE, codec_name(info.codec) or "?", Diagnosis.OK if info.codec in rtsp.DECODABLE else Diagnosis.UNSUPPORTED_CODEC))
        report.add(Check("resolution", True, f"{info.width}x{info.height}"))
        return ProbeResult(report, {"connection_type": "hls", "video": True, "audio": bool(info.audio), "h264": info.codec == "h264", "h265": info.codec in ("hevc", "h265"),
                                    "mainstream": True, "substream": False, "ptz": None, "events": None, "recording": None},
                           [ChannelInfo("1", "Stream", [StreamProfile("main", self.url, codec_name(info.codec), info.width, info.height, info.fps, bool(info.audio))])])

    def spec(self) -> dict[str, Any]:
        return {"type": "hls", "url": self.url}


def build_connection(spec: dict[str, Any], username: str | None = None, password: str | None = None) -> CameraConnection:
    """From an Add Camera request or a stored spec to a connection object."""
    kind = spec.get("type")
    if kind == "rtsp":
        return RTSPConnection(spec["url"], username, password, spec.get("sub_url"))
    if kind == "onvif":
        return ONVIFConnection(spec["xaddr"], username, password)
    if kind == "nvr":
        return NVRConnection(spec["xaddr"], username, password)
    if kind == "hls":
        return HLSConnection(spec["url"])
    if kind == "vendor":
        from app.cameras.vendors import get_vendor

        return get_vendor(spec["vendor"]).connection(spec, username, password)
    raise ValueError(f"Unknown connection type {kind!r}")


def stream_url(uri: str, username: str | None, password: str | None) -> str:
    return with_credentials(uri, username, password) if uri.startswith(("rtsp://", "rtsps://")) else uri
