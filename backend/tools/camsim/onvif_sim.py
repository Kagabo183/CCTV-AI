"""Minimal ONVIF device simulator for testing Visionary's ONVIF client (NOT a full ONVIF implementation).

Two devices, each on its own HTTP port:
  18080  "Simulated Hikvision DS-2CD2143G2" camera  Profile T (Media2) + Profile S (Media1), 2 profiles (main/sub)
  18081  "Simulated Dahua NVR4104"                   Profile S (Media1), 4 channels

Implements: GetSystemDateAndTime (no auth), GetDeviceInformation, GetCapabilities, GetServices, GetScopes,
Media1 GetProfiles/GetStreamUri/GetSnapshotUri, Media2 GetProfiles/GetStreamUri, PTZ (camera only:
ContinuousMove/Stop/GetStatus/GetPresets/GotoPreset on a simulated position), Events (camera only: a pull point
whose motion alarm toggles every 15 s: CreatePullPointSubscription/PullMessages), and WS-Discovery Probe
responses on UDP 239.255.255.250:3702. Requests are authenticated with WS-Security UsernameToken
PasswordDigest, as real cameras do; a wrong password returns the ter:NotAuthorized SOAP fault.
Stream URIs point at the simulator's MediaMTX (rtsp://127.0.0.1:18554).
"""

from __future__ import annotations

import base64
import hashlib
import re
import socket
import struct
import threading
import time
import uuid
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Request, Response

USER, PASSWORD = "admin", "Cam#2026!"
RTSP = "rtsp://127.0.0.1:18554"

DEVICES = {
    18080: {
        "uuid": "urn:uuid:5f5a69c2-e0ae-504f-829b-00010f4e1f11",
        "manufacturer": "Simulated Hikvision", "model": "DS-2CD2143G2-I", "firmware": "V5.7.3 build 220112", "serial": "DS-2CD2143G2-I20220101AAWRJ12345678",
        "profile_t": True, "ptz": True, "events": True, "audio": True,
        "profiles": [
            {"token": "Profile_101", "name": "mainStream", "path": "Streaming/Channels/101", "enc": "H264", "w": 1920, "h": 1080, "fps": 30, "source": "VideoSource_1"},
            {"token": "Profile_102", "name": "subStream", "path": "Streaming/Channels/102", "enc": "H264", "w": 640, "h": 360, "fps": 30, "source": "VideoSource_1"},
        ],
    },
    18081: {
        "uuid": "urn:uuid:7c1d0e2a-9b8f-4c3e-a1d2-3e4f5a6b7c8d",
        "manufacturer": "Simulated Dahua", "model": "NVR4104-4KS2", "firmware": "4.001.0000000.6", "serial": "7K0A1B2C3D4E5F",
        "profile_t": False, "ptz": False, "events": False, "audio": False,
        "profiles": [
            {"token": f"MediaProfile00{i}", "name": f"Channel {i}", "path": f"nvr/ch{i}", "enc": "H264", "w": w, "h": h, "fps": 30, "source": f"VideoSource_{i}"}
            for i, (w, h) in enumerate([(640, 360), (1280, 720), (1280, 720), (1280, 720)], start=1)
        ],
    },
}

ENV = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"
 xmlns:ter="http://www.onvif.org/ver10/error" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
 xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:wsa="http://www.w3.org/2005/08/addressing" xmlns:tns1="http://www.onvif.org/ver10/topics"><s:Body>{body}</s:Body></s:Envelope>"""


class PTZState:
    """Position in ONVIF generic space (-1..1 pan/tilt, 0..1 zoom), integrated from ContinuousMove velocities."""

    def __init__(self) -> None:
        self.pos = [0.0, 0.0, 0.0]
        self.vel = [0.0, 0.0, 0.0]
        self.since = time.monotonic()
        self.presets = {"1": ("Home", [0.0, 0.0, 0.0]), "2": ("Gate", [0.6, -0.2, 0.3])}

    def settle(self) -> None:
        dt, self.since = time.monotonic() - self.since, time.monotonic()
        lim = [(-1, 1), (-1, 1), (0, 1)]
        self.pos = [max(lo, min(hi, p + v * dt * 0.5)) for p, v, (lo, hi) in zip(self.pos, self.vel, lim)]


def motion_now() -> bool:
    return int(time.time() // 15) % 2 == 1

FAULT = """<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>
<s:Reason><s:Text xml:lang="en">Sender not Authorized</s:Text></s:Reason></s:Fault>"""


def _authorized(xml: str) -> bool:
    user = re.search(r"<(?:\w+:)?Username>([^<]*)<", xml)
    digest = re.search(r"<(?:\w+:)?Password[^>]*>([^<]*)<", xml)
    nonce = re.search(r"<(?:\w+:)?Nonce[^>]*>([^<]*)<", xml)
    created = re.search(r"<(?:\w+:)?Created>([^<]*)<", xml)
    if not (user and digest and nonce and created) or user.group(1) != USER:
        return False
    expected = base64.b64encode(hashlib.sha1(base64.b64decode(nonce.group(1)) + created.group(1).encode() + PASSWORD.encode()).digest()).decode()
    return expected == digest.group(1)


def _profiles_media1(dev: dict) -> str:
    out = []
    for p in dev["profiles"]:
        out.append(f"""<trt:Profiles token="{p['token']}" fixed="true"><tt:Name>{p['name']}</tt:Name>
<tt:VideoSourceConfiguration token="VSC_{p['source']}"><tt:Name>{p['source']}</tt:Name><tt:SourceToken>{p['source']}</tt:SourceToken></tt:VideoSourceConfiguration>
<tt:VideoEncoderConfiguration token="VEC_{p['token']}"><tt:Name>{p['name']}</tt:Name><tt:Encoding>{p['enc']}</tt:Encoding>
<tt:Resolution><tt:Width>{p['w']}</tt:Width><tt:Height>{p['h']}</tt:Height></tt:Resolution>
<tt:RateControl><tt:FrameRateLimit>{p['fps']}</tt:FrameRateLimit><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl></tt:VideoEncoderConfiguration>
{'<tt:AudioEncoderConfiguration token="AEC"><tt:Name>audio</tt:Name><tt:Encoding>G711</tt:Encoding></tt:AudioEncoderConfiguration>' if dev['audio'] else ''}
</trt:Profiles>""")
    return "<trt:GetProfilesResponse>" + "".join(out) + "</trt:GetProfilesResponse>"


def _profiles_media2(dev: dict) -> str:
    out = []
    for p in dev["profiles"]:
        out.append(f"""<tr2:Profiles token="{p['token']}" fixed="true"><tr2:Name>{p['name']}</tr2:Name><tr2:Configurations>
<tr2:VideoSource token="VSC_{p['source']}"><tt:Name>{p['source']}</tt:Name><tt:SourceToken>{p['source']}</tt:SourceToken></tr2:VideoSource>
<tr2:VideoEncoder token="VEC_{p['token']}" GovLength="30" Profile="Main"><tt:Name>{p['name']}</tt:Name><tt:Encoding>{p['enc']}</tt:Encoding>
<tt:Resolution><tt:Width>{p['w']}</tt:Width><tt:Height>{p['h']}</tt:Height></tt:Resolution>
<tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>{p['fps']}</tt:FrameRateLimit><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl></tr2:VideoEncoder>
{'<tr2:AudioEncoder token="AEC"><tt:Name>audio</tt:Name><tt:Encoding>PCMU</tt:Encoding></tr2:AudioEncoder>' if dev['audio'] else ''}
</tr2:Configurations></tr2:Profiles>""")
    return "<tr2:GetProfilesResponse>" + "".join(out) + "</tr2:GetProfilesResponse>"


def make_app(port: int) -> FastAPI:
    dev = DEVICES[port]
    base = f"http://127.0.0.1:{port}/onvif"
    app = FastAPI()
    ptz = PTZState()
    last_motion: dict[str, bool | None] = {}  # pull point -> last reported state

    @app.post("/onvif/{service}")
    async def soap(service: str, request: Request) -> Response:
        xml = (await request.body()).decode("utf-8", "replace")
        action = re.search(r"<(?:\w+:)?Body[^>]*>\s*<(?:\w+:)?(\w+)", xml)
        name = action.group(1) if action else ""
        if name != "GetSystemDateAndTime" and not _authorized(xml):
            return Response(ENV.format(body=FAULT), status_code=400, media_type="application/soap+xml")
        now = datetime.now(UTC)
        if name == "GetSystemDateAndTime":
            body = f"""<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType><tt:UTCDateTime>
<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>
<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"""
        elif name == "GetDeviceInformation":
            body = f"""<tds:GetDeviceInformationResponse><tds:Manufacturer>{dev['manufacturer']}</tds:Manufacturer><tds:Model>{dev['model']}</tds:Model>
<tds:FirmwareVersion>{dev['firmware']}</tds:FirmwareVersion><tds:SerialNumber>{dev['serial']}</tds:SerialNumber><tds:HardwareId>SIM</tds:HardwareId></tds:GetDeviceInformationResponse>"""
        elif name == "GetServices":
            media2 = f'<tds:Service><tds:Namespace>http://www.onvif.org/ver20/media/wsdl</tds:Namespace><tds:XAddr>{base}/media2_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>' if dev["profile_t"] else ""
            body = f"""<tds:GetServicesResponse>
<tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace><tds:XAddr>{base}/device_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>
<tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace><tds:XAddr>{base}/media_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>
{media2}
{'<tds:Service><tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace><tds:XAddr>' + base + '/ptz_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>' if dev["ptz"] else ''}
<tds:Service><tds:Namespace>http://www.onvif.org/ver10/events/wsdl</tds:Namespace><tds:XAddr>{base}/event_service</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>
</tds:GetServicesResponse>"""
        elif name == "GetCapabilities":
            body = f"""<tds:GetCapabilitiesResponse><tds:Capabilities>
<tt:Device><tt:XAddr>{base}/device_service</tt:XAddr></tt:Device>
<tt:Events><tt:XAddr>{base}/event_service</tt:XAddr><tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport><tt:WSPullPointSupport>true</tt:WSPullPointSupport></tt:Events>
<tt:Media><tt:XAddr>{base}/media_service</tt:XAddr><tt:StreamingCapabilities><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>
</tds:Capabilities></tds:GetCapabilitiesResponse>"""
        elif name == "GetScopes":
            profiles = ["S"] + (["T"] if dev["profile_t"] else [])
            scopes = "".join(f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/Profile/{p}</tt:ScopeItem></tds:Scopes>" for p in profiles)
            body = f"<tds:GetScopesResponse>{scopes}<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/hardware/{dev['model']}</tt:ScopeItem></tds:Scopes></tds:GetScopesResponse>"
        elif name == "GetProfiles":
            body = _profiles_media2(dev) if service == "media2_service" else _profiles_media1(dev)
        elif name in ("GetStreamUri", "GetSnapshotUri"):
            token = re.search(r"<(?:\w+:)?ProfileToken>([^<]*)<", xml)
            prof = next((p for p in dev["profiles"] if token and p["token"] == token.group(1)), dev["profiles"][0])
            if name == "GetSnapshotUri":
                body = f"<trt:GetSnapshotUriResponse><trt:MediaUri><tt:Uri>http://127.0.0.1:{port}/snapshot/{prof['token']}.jpg</tt:Uri></trt:MediaUri></trt:GetSnapshotUriResponse>"
            elif service == "media2_service":
                body = f"<tr2:GetStreamUriResponse><tr2:Uri>{RTSP}/{prof['path']}</tr2:Uri></tr2:GetStreamUriResponse>"
            else:
                body = f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{RTSP}/{prof['path']}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>"
        elif name == "ContinuousMove" and dev["ptz"]:
            ptz.settle()
            pt = re.search(r'PanTilt[^>]*x="([-\d.]+)"[^>]*y="([-\d.]+)"', xml)
            z = re.search(r'Zoom[^>]*x="([-\d.]+)"', xml)
            ptz.vel = [float(pt.group(1)) if pt else 0.0, float(pt.group(2)) if pt else 0.0, float(z.group(1)) if z else 0.0]
            body = "<tptz:ContinuousMoveResponse/>"
        elif name == "Stop" and dev["ptz"]:
            ptz.settle()
            ptz.vel = [0.0, 0.0, 0.0]
            body = "<tptz:StopResponse/>"
        elif name == "GetStatus" and dev["ptz"]:
            ptz.settle()
            moving = "MOVING" if any(ptz.vel) else "IDLE"
            body = (f'<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position><tt:PanTilt x="{ptz.pos[0]:.3f}" y="{ptz.pos[1]:.3f}"/><tt:Zoom x="{ptz.pos[2]:.3f}"/></tt:Position>'
                    f'<tt:MoveStatus><tt:PanTilt>{moving}</tt:PanTilt><tt:Zoom>{moving}</tt:Zoom></tt:MoveStatus></tptz:PTZStatus></tptz:GetStatusResponse>')
        elif name == "GetPresets" and dev["ptz"]:
            body = "<tptz:GetPresetsResponse>" + "".join(f'<tptz:Preset token="{t}"><tt:Name>{n}</tt:Name></tptz:Preset>' for t, (n, _) in ptz.presets.items()) + "</tptz:GetPresetsResponse>"
        elif name == "GotoPreset" and dev["ptz"]:
            tok = re.search(r"<(?:\w+:)?PresetToken>([^<]*)<", xml)
            ptz.settle()
            ptz.vel = [0.0, 0.0, 0.0]
            ptz.pos = list(ptz.presets.get(tok.group(1) if tok else "", ("", ptz.pos))[1])
            body = "<tptz:GotoPresetResponse/>"
        elif name == "CreatePullPointSubscription" and dev["events"]:
            sub = uuid.uuid4().hex[:12]
            last_motion[sub] = None
            body = (f"<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference><wsa:Address>{base}/pullpoint_{sub}</wsa:Address></tev:SubscriptionReference>"
                    f"<wsnt:CurrentTime>{now.isoformat()}</wsnt:CurrentTime><wsnt:TerminationTime>{now.isoformat()}</wsnt:TerminationTime></tev:CreatePullPointSubscriptionResponse>")
        elif name == "PullMessages" and service.startswith("pullpoint_") and service[10:] in last_motion:
            sub = service[10:]
            state = motion_now()
            msgs = ""
            if last_motion[sub] != state:  # like a camera: report property changes (first pull = initial state)
                op = "Initialized" if last_motion[sub] is None else "Changed"
                last_motion[sub] = state
                msgs = (f'<wsnt:NotificationMessage><wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>'
                        f'<wsnt:Message><tt:Message UtcTime="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}" PropertyOperation="{op}">'
                        f'<tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="VSC_VideoSource_1"/><tt:SimpleItem Name="Rule" Value="MyMotionDetectorRule"/></tt:Source>'
                        f'<tt:Data><tt:SimpleItem Name="IsMotion" Value="{str(state).lower()}"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>')
            body = f"<tev:PullMessagesResponse><tev:CurrentTime>{now.isoformat()}</tev:CurrentTime><tev:TerminationTime>{now.isoformat()}</tev:TerminationTime>{msgs}</tev:PullMessagesResponse>"
        else:
            body = f'<s:Fault><s:Code><s:Value>s:Receiver</s:Value><s:Subcode><s:Value>ter:ActionNotSupported</s:Value></s:Subcode></s:Code><s:Reason><s:Text xml:lang="en">{name} not supported by the simulator</s:Text></s:Reason></s:Fault>'
            return Response(ENV.format(body=body), status_code=400, media_type="application/soap+xml")
        return Response(ENV.format(body=body), media_type="application/soap+xml")

    return app


def ws_discovery() -> None:
    """Answer WS-Discovery Probe messages for both simulated devices."""
    group = "239.255.255.250"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 3702))
    ifaces = {"127.0.0.1"} | {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    for ip in ifaces:  # listen on every interface, like a camera on the LAN
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(ip)))
        except OSError:
            pass
    while True:
        data, addr = sock.recvfrom(65535)
        text = data.decode("utf-8", "replace")
        if "Probe" not in text or "ProbeMatches" in text:
            continue
        relates = re.search(r"<(?:\w+:)?MessageID>([^<]*)<", text)
        for port, dev in DEVICES.items():
            scopes = " ".join([f"onvif://www.onvif.org/name/{dev['model']}", f"onvif://www.onvif.org/hardware/{dev['model']}",
                               "onvif://www.onvif.org/Profile/Streaming"] + (["onvif://www.onvif.org/Profile/T"] if dev["profile_t"] else []))
            reply = f"""<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header><a:MessageID>uuid:{uuid.uuid4()}</a:MessageID>
<a:RelatesTo>{relates.group(1) if relates else ''}</a:RelatesTo><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action></s:Header>
<s:Body><d:ProbeMatches><d:ProbeMatch><a:EndpointReference><a:Address>{dev['uuid']}</a:Address></a:EndpointReference><d:Types>dn:NetworkVideoTransmitter</d:Types>
<d:Scopes>{scopes}</d:Scopes><d:XAddrs>http://127.0.0.1:{port}/onvif/device_service</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"""
            sock.sendto(reply.encode(), addr)


if __name__ == "__main__":
    threading.Thread(target=ws_discovery, daemon=True).start()
    servers = [uvicorn.Server(uvicorn.Config(make_app(port), host="127.0.0.1", port=port, log_level="warning")) for port in DEVICES]
    threads = [threading.Thread(target=s.run, daemon=True) for s in servers[1:]]
    for t in threads:
        t.start()
    servers[0].run()
