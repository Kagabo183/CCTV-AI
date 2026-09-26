"""ONVIF client: discovery, device information, capabilities, media profiles and stream URIs.

A small SOAP client (no zeep): WS-Security UsernameToken with PasswordDigest, Media2 (ver20, Profile T) when
the device offers it, else Media1 (ver10, Profile S). Nothing is assumed: every capability comes from the
device's own answers (GetServices, GetCapabilities, GetScopes, GetProfiles).

Discovery is WS-Discovery on UDP 239.255.255.250:3702 and only runs on networks the operator allowed
(CAMERA_PRIVATE_NETWORKS / a gateway's own LAN): it never scans Internet ranges.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.cameras.diagnostics import Check, Diagnosis, ProbeReport

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa5": "http://www.w3.org/2005/08/addressing",
}
MEDIA1, MEDIA2 = "http://www.onvif.org/ver10/media/wsdl", "http://www.onvif.org/ver20/media/wsdl"
PTZ_NS, EVENTS_NS, RECORDING_NS, IMAGING_NS = (
    "http://www.onvif.org/ver20/ptz/wsdl", "http://www.onvif.org/ver10/events/wsdl",
    "http://www.onvif.org/ver10/recording/wsdl", "http://www.onvif.org/ver20/imaging/wsdl")


class OnvifError(Exception):
    def __init__(self, code: Diagnosis, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class OnvifProfile:
    token: str
    name: str
    encoding: str | None
    width: int | None
    height: int | None
    fps: float | None
    source: str | None  # video source token: profiles sharing one are streams of the same channel
    audio: bool = False
    stream_uri: str | None = None


@dataclass
class OnvifDevice:
    xaddr: str
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    hardware_id: str | None = None
    profiles_supported: list[str] = field(default_factory=list)  # S, T, G, M ...
    services: dict[str, str] = field(default_factory=dict)  # namespace -> xaddr
    media_version: int = 1
    profiles: list[OnvifProfile] = field(default_factory=list)
    snapshot_uri: str | None = None

    @property
    def channels(self) -> dict[str, list[OnvifProfile]]:
        out: dict[str, list[OnvifProfile]] = {}
        for p in self.profiles:
            out.setdefault(p.source or p.token, []).append(p)
        return out

    def capabilities(self) -> dict[str, Any]:
        encs = {(p.encoding or "").upper() for p in self.profiles}
        return {
            "connection_type": "onvif",
            "manufacturer": self.manufacturer, "model": self.model, "firmware": self.firmware, "serial": self.serial,
            "onvif": True, "onvif_profiles": self.profiles_supported, "profile_t": "T" in self.profiles_supported,
            "video": bool(self.profiles), "audio": any(p.audio for p in self.profiles),
            "h264": "H264" in encs, "h265": bool({"H265", "HEVC"} & encs),
            "ptz": PTZ_NS in self.services, "events": EVENTS_NS in self.services,
            "recording": RECORDING_NS in self.services, "imaging": IMAGING_NS in self.services,
            "snapshot": bool(self.snapshot_uri),
            "channels": len(self.channels),
            "mainstream": bool(self.profiles), "substream": any(len(v) > 1 for v in self.channels.values()),
        }


def _security_header(user: str, password: str, clock_offset: timedelta) -> str:
    nonce = os.urandom(16)
    created = (datetime.now(UTC) + clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return f"""<s:Header><wsse:Security s:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"><wsse:UsernameToken><wsse:Username>{_xml(user)}</wsse:Username>
<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>
<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce>
<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>"""


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text else None


class OnvifClient:
    def __init__(self, xaddr: str, username: str | None, password: str | None, timeout: float = 6.0) -> None:
        self.xaddr = xaddr
        self.username, self.password = username, password
        self.timeout = timeout
        self.clock_offset = timedelta(0)

    async def call(self, xaddr: str, body: str, *, auth: bool = True, action: str | None = None) -> ET.Element:
        header = _security_header(self.username, self.password or "", self.clock_offset) if auth and self.username else ""
        if action:  # pull-point subscriptions on many devices route by WS-Addressing
            addressing = f'<wsa5:Action>{_xml(action)}</wsa5:Action><wsa5:To>{_xml(xaddr)}</wsa5:To>'
            header = header.replace("<s:Header>", "<s:Header>" + addressing, 1) if header else f"<s:Header>{addressing}</s:Header>"
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{NS['s']}" xmlns:tds="{NS['tds']}" xmlns:trt="{NS['trt']}"
xmlns:tr2="{NS['tr2']}" xmlns:tt="{NS['tt']}" xmlns:tptz="{NS['tptz']}" xmlns:tev="{NS['tev']}" xmlns:wsnt="{NS['wsnt']}"
xmlns:wsa5="{NS['wsa5']}">{header}<s:Body>{body}</s:Body></s:Envelope>"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                r = await client.post(xaddr, content=envelope.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        except httpx.ConnectError as exc:
            raise OnvifError(Diagnosis.ONVIF_DISABLED, f"nothing answers ONVIF at {xaddr}") from exc
        except httpx.TimeoutException as exc:
            raise OnvifError(Diagnosis.CAMERA_OFFLINE, f"no ONVIF answer from {xaddr}") from exc
        if r.status_code == 401 or "NotAuthorized" in r.text:
            raise OnvifError(Diagnosis.AUTHENTICATION_FAILED if self.username else Diagnosis.INVALID_CREDENTIALS, "ONVIF login rejected")
        if r.status_code == 404:
            raise OnvifError(Diagnosis.ONVIF_DISABLED, f"no ONVIF service at {xaddr}")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            raise OnvifError(Diagnosis.ONVIF_DISABLED, "the device did not answer with ONVIF SOAP") from exc
        fault = root.find(".//s:Fault", NS)
        if fault is not None:
            reason = "".join(fault.itertext()).strip()[:160]
            raise OnvifError(Diagnosis.UNKNOWN_ERROR, f"ONVIF fault: {reason}")
        return root

    async def sync_clock(self) -> None:
        """Digest auth depends on time: measure the camera's clock offset (GetSystemDateAndTime needs no auth)."""
        root = await self.call(self.xaddr, "<tds:GetSystemDateAndTime/>", auth=False)
        utc = root.find(".//tt:UTCDateTime", NS)
        if utc is None:
            return
        try:
            cam = datetime(int(_text(utc, "tt:Date/tt:Year")), int(_text(utc, "tt:Date/tt:Month")), int(_text(utc, "tt:Date/tt:Day")),
                           int(_text(utc, "tt:Time/tt:Hour")), int(_text(utc, "tt:Time/tt:Minute")), int(_text(utc, "tt:Time/tt:Second")), tzinfo=UTC)
            self.clock_offset = cam - datetime.now(UTC)
        except (TypeError, ValueError):
            pass

    async def device(self) -> OnvifDevice:
        dev = OnvifDevice(self.xaddr)
        info = (await self.call(self.xaddr, "<tds:GetDeviceInformation/>")).find(".//tds:GetDeviceInformationResponse", NS)
        dev.manufacturer, dev.model = _text(info, "tds:Manufacturer"), _text(info, "tds:Model")
        dev.firmware, dev.serial, dev.hardware_id = _text(info, "tds:FirmwareVersion"), _text(info, "tds:SerialNumber"), _text(info, "tds:HardwareId")
        try:
            root = await self.call(self.xaddr, "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
            for svc in root.findall(".//tds:Service", NS):
                ns, addr = _text(svc, "tds:Namespace"), _text(svc, "tds:XAddr")
                if ns and addr:
                    dev.services[ns] = addr
        except OnvifError:
            root = await self.call(self.xaddr, "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
            for tag, ns in (("Media", MEDIA1), ("Events", EVENTS_NS), ("PTZ", PTZ_NS), ("Imaging", IMAGING_NS)):
                addr = _text(root, f".//tt:{tag}/tt:XAddr")
                if addr:
                    dev.services[ns] = addr
        try:
            scopes = await self.call(self.xaddr, "<tds:GetScopes/>")
            items = [s.text or "" for s in scopes.iter(f"{{{NS['tt']}}}ScopeItem")]
            dev.profiles_supported = sorted({m.group(1) for s in items if (m := re.search(r"/Profile/([A-Z])\b", s))})
        except OnvifError:
            pass
        if MEDIA2 in dev.services:
            dev.media_version = 2
            dev.profiles = await self._profiles_media2(dev.services[MEDIA2])
        elif MEDIA1 in dev.services:
            dev.profiles = await self._profiles_media1(dev.services[MEDIA1])
        return dev

    async def _profiles_media1(self, addr: str) -> list[OnvifProfile]:
        root = await self.call(addr, "<trt:GetProfiles/>")
        out = []
        for p in root.findall(".//trt:Profiles", NS):
            vec = p.find("tt:VideoEncoderConfiguration", NS)
            fps = _text(vec, "tt:RateControl/tt:FrameRateLimit")
            out.append(OnvifProfile(p.get("token", ""), _text(p, "tt:Name") or p.get("token", ""), _text(vec, "tt:Encoding"),
                                    int(_text(vec, "tt:Resolution/tt:Width") or 0) or None, int(_text(vec, "tt:Resolution/tt:Height") or 0) or None,
                                    float(fps) if fps else None, _text(p, "tt:VideoSourceConfiguration/tt:SourceToken"),
                                    p.find("tt:AudioEncoderConfiguration", NS) is not None))
        for prof in out:
            body = f"""<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>
<trt:ProfileToken>{_xml(prof.token)}</trt:ProfileToken></trt:GetStreamUri>"""
            prof.stream_uri = _text(await self.call(addr, body), ".//trt:MediaUri/tt:Uri")
        return out

    async def _profiles_media2(self, addr: str) -> list[OnvifProfile]:
        root = await self.call(addr, "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>")
        out = []
        for p in root.findall(".//tr2:Profiles", NS):
            conf = p.find("tr2:Configurations", NS)
            vec = conf.find("tr2:VideoEncoder", NS) if conf is not None else None
            fps = _text(vec, "tt:RateControl/tt:FrameRateLimit")
            out.append(OnvifProfile(p.get("token", ""), _text(p, "tr2:Name") or p.get("token", ""), _text(vec, "tt:Encoding"),
                                    int(_text(vec, "tt:Resolution/tt:Width") or 0) or None, int(_text(vec, "tt:Resolution/tt:Height") or 0) or None,
                                    float(fps) if fps else None, _text(conf, "tr2:VideoSource/tt:SourceToken") if conf is not None else None,
                                    conf is not None and conf.find("tr2:AudioEncoder", NS) is not None))
        for prof in out:
            body = f"<tr2:GetStreamUri><tr2:Protocol>RtspUnicast</tr2:Protocol><tr2:ProfileToken>{_xml(prof.token)}</tr2:ProfileToken></tr2:GetStreamUri>"
            prof.stream_uri = _text(await self.call(addr, body), ".//tr2:Uri")
        return out

    # ---------------------------------------------------------------- PTZ (only when the device has a PTZ service)
    async def ptz_move(self, ptz_xaddr: str, profile_token: str, pan: float, tilt: float, zoom: float) -> None:
        clamp = lambda v: max(-1.0, min(1.0, float(v)))  # noqa: E731 - ONVIF generic velocity space is -1..1
        await self.call(ptz_xaddr, f"""<tptz:ContinuousMove><tptz:ProfileToken>{_xml(profile_token)}</tptz:ProfileToken><tptz:Velocity>
<tt:PanTilt x="{clamp(pan)}" y="{clamp(tilt)}"/><tt:Zoom x="{clamp(zoom)}"/></tptz:Velocity></tptz:ContinuousMove>""")

    async def ptz_stop(self, ptz_xaddr: str, profile_token: str) -> None:
        await self.call(ptz_xaddr, f"<tptz:Stop><tptz:ProfileToken>{_xml(profile_token)}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>")

    async def ptz_presets(self, ptz_xaddr: str, profile_token: str) -> list[dict[str, str]]:
        root = await self.call(ptz_xaddr, f"<tptz:GetPresets><tptz:ProfileToken>{_xml(profile_token)}</tptz:ProfileToken></tptz:GetPresets>")
        return [{"token": p.get("token", ""), "name": _text(p, "tt:Name") or p.get("token", "")} for p in root.iter(f"{{{NS['tptz']}}}Preset")]

    async def ptz_goto(self, ptz_xaddr: str, profile_token: str, preset: str) -> None:
        await self.call(ptz_xaddr, f"<tptz:GotoPreset><tptz:ProfileToken>{_xml(profile_token)}</tptz:ProfileToken><tptz:PresetToken>{_xml(preset)}</tptz:PresetToken></tptz:GotoPreset>")

    async def ptz_status(self, ptz_xaddr: str, profile_token: str) -> dict[str, float | None]:
        root = await self.call(ptz_xaddr, f"<tptz:GetStatus><tptz:ProfileToken>{_xml(profile_token)}</tptz:ProfileToken></tptz:GetStatus>")
        pt, z = root.find(".//tt:Position/tt:PanTilt", NS), root.find(".//tt:Position/tt:Zoom", NS)
        f = lambda el, k: float(el.get(k)) if el is not None and el.get(k) is not None else None  # noqa: E731
        return {"pan": f(pt, "x"), "tilt": f(pt, "y"), "zoom": f(z, "x")}

    # ---------------------------------------------------------------- events (pull point, ONVIF Core / Profile T)
    async def create_pull_point(self, events_xaddr: str, seconds: int = 600) -> str:
        root = await self.call(events_xaddr, f"<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT{seconds}S</tev:InitialTerminationTime></tev:CreatePullPointSubscription>")
        addr = root.find(".//tev:SubscriptionReference/wsa5:Address", NS)
        if addr is None:
            addr = root.find(".//tev:SubscriptionReference/{http://schemas.xmlsoap.org/ws/2004/08/addressing}Address")
        if addr is None or not (addr.text or "").strip():
            raise OnvifError(Diagnosis.UNKNOWN_ERROR, "the device did not return a pull-point address")
        return addr.text.strip()

    async def pull_messages(self, pull_xaddr: str, timeout: int = 5, limit: int = 20) -> list[dict[str, Any]]:
        root = await self.call(pull_xaddr, f"<tev:PullMessages><tev:Timeout>PT{timeout}S</tev:Timeout><tev:MessageLimit>{limit}</tev:MessageLimit></tev:PullMessages>",
                               action="http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest")
        out = []
        for msg in root.iter(f"{{{NS['wsnt']}}}NotificationMessage"):
            topic_el = msg.find("wsnt:Topic", NS)
            topic = "".join(topic_el.itertext()).strip() if topic_el is not None else ""
            inner = msg.find(".//tt:Message", NS)
            items = lambda part: {i.get("Name", ""): i.get("Value", "") for i in (inner.findall(f"tt:{part}/tt:SimpleItem", NS) if inner is not None else [])}  # noqa: E731
            out.append({"topic": topic, "utc": inner.get("UtcTime") if inner is not None else None, "operation": inner.get("PropertyOperation") if inner is not None else None,
                        "source": items("Source"), "data": items("Data")})
        return out

    async def snapshot_uri(self, dev: OnvifDevice) -> str | None:
        if not dev.profiles or MEDIA1 not in dev.services:
            return None
        try:
            root = await self.call(dev.services[MEDIA1], f"<trt:GetSnapshotUri><trt:ProfileToken>{_xml(dev.profiles[0].token)}</trt:ProfileToken></trt:GetSnapshotUri>")
            return _text(root, ".//trt:MediaUri/tt:Uri")
        except OnvifError:
            return None


def device_url(xaddr: str) -> str:
    """'192.168.1.20' or '192.168.1.20:8080' -> the standard device service URL; full URLs are kept."""
    xaddr = xaddr.strip()
    if "://" not in xaddr:
        xaddr = f"http://{xaddr}"
    parts = urlsplit(xaddr)
    return xaddr if parts.path not in ("", "/") else f"{parts.scheme}://{parts.netloc}/onvif/device_service"


async def probe(xaddr: str, username: str | None, password: str | None) -> tuple[ProbeReport, OnvifDevice | None]:
    report = ProbeReport()
    xaddr = device_url(xaddr)
    host = urlsplit(xaddr).hostname or ""
    t0 = time.perf_counter()
    client = OnvifClient(xaddr, username, password)
    try:
        await client.sync_clock()
        report.add(Check("reachable", True, host, ms=round((time.perf_counter() - t0) * 1000, 1)))
        report.add(Check("onvif", True, "ONVIF device service answers"))
    except OnvifError as exc:
        report.add(Check("onvif", False, exc.detail, exc.code))
        return report, None
    try:
        dev = await client.device()
    except OnvifError as exc:
        report.add(Check("authentication", False, exc.detail, exc.code))
        return report, None
    report.add(Check("authentication", True, "accepted"))
    dev.snapshot_uri = await client.snapshot_uri(dev)
    if not dev.profiles:
        report.add(Check("stream", False, "the device reports no media profiles", Diagnosis.STREAM_NOT_FOUND))
        return report, dev
    profile_t = "T" in dev.profiles_supported
    report.add(Check("profiles", True, f"{len(dev.profiles)} media profile(s), {len(dev.channels)} channel(s); ONVIF profiles {', '.join(dev.profiles_supported) or 'not reported'}"
                     + (" · Profile T" if profile_t else "")))
    return report, dev


# ---------------------------------------------------------------------------------------------- discovery
@dataclass
class Discovered:
    address: str  # endpoint reference (stable device UUID)
    xaddrs: list[str]
    scopes: list[str]
    ip: str

    @property
    def name(self) -> str | None:
        for s in self.scopes:
            if "/name/" in s:
                return s.rsplit("/name/", 1)[1].replace("%20", " ")
        return None

    @property
    def hardware(self) -> str | None:
        for s in self.scopes:
            if "/hardware/" in s:
                return s.rsplit("/hardware/", 1)[1].replace("%20", " ")
        return None


PROBE = """<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header><a:MessageID>uuid:{id}</a:MessageID>
<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action></s:Header>
<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>"""


def local_ipv4() -> list[str]:
    """IPv4 addresses of this machine's interfaces (discovery probes go out on each one)."""
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    return sorted(ips)


def discover(timeout: float = 3.0, interfaces: list[str] | None = None) -> list[Discovered]:
    """WS-Discovery Probe on the local network segments (blocking; run in a thread).

    A probe is sent out of every local interface (Windows only multicasts on one interface otherwise).
    Multicast TTL 2 keeps it on the local network: this never scans remote or Internet ranges."""
    found: dict[str, Discovered] = {}
    socks = []
    message = PROBE.format(id=uuid.uuid4()).encode()
    for ip in interfaces or local_ipv4():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sock.bind((ip, 0))
            sock.setblocking(False)
            sock.sendto(message, ("239.255.255.250", 3702))
            socks.append(sock)
        except OSError:
            continue
    deadline = time.monotonic() + timeout
    import select

    try:
        while time.monotonic() < deadline and socks:
            ready, _, _ = select.select(socks, [], [], 0.2)
            for sock in ready:
                try:
                    data, (ip, _) = sock.recvfrom(65535)
                except OSError:
                    continue
                try:
                    root = ET.fromstring(data)
                except ET.ParseError:
                    continue
                for match in root.iter(f"{{{NS['d']}}}ProbeMatch"):
                    addr = _text(match, "a:EndpointReference/a:Address") or ip
                    xaddrs = (_text(match, "d:XAddrs") or "").split()
                    scopes = (_text(match, "d:Scopes") or "").split()
                    if xaddrs:
                        found[addr] = Discovered(addr, xaddrs, scopes, urlsplit(xaddrs[0]).hostname or ip)
    finally:
        for sock in socks:
            sock.close()
    _ = struct
    return list(found.values())


async def discover_async(timeout: float = 3.0) -> list[Discovered]:
    return await asyncio.to_thread(discover, timeout)
