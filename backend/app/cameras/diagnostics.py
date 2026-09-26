"""Why a camera does or does not connect, in words a user can act on.

Every probe produces a list of checks (DNS, reachability, ONVIF, RTSP, authentication, codec, ...).
A failed check carries a Diagnosis code with an explanation and a suggested next step, never just
"connection failed".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Diagnosis(str, Enum):
    OK = "OK"
    DNS_FAILED = "DNS_FAILED"
    NETWORK_UNREACHABLE = "NETWORK_UNREACHABLE"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    NOT_ALLOWED_NETWORK = "NOT_ALLOWED_NETWORK"
    ONVIF_DISABLED = "ONVIF_DISABLED"
    RTSP_DISABLED = "RTSP_DISABLED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    LINK_EXPIRED = "LINK_EXPIRED"  # a link with its own access token (?token=...) was refused
    STREAM_NOT_FOUND = "STREAM_NOT_FOUND"
    STREAM_TIMEOUT = "STREAM_TIMEOUT"
    UNSUPPORTED_CODEC = "UNSUPPORTED_CODEC"
    GATEWAY_OFFLINE = "GATEWAY_OFFLINE"
    PROPRIETARY_PROTOCOL = "PROPRIETARY_PROTOCOL"
    PROVIDER_API_UNAVAILABLE = "PROVIDER_API_UNAVAILABLE"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


GUIDANCE: dict[Diagnosis, tuple[str, str]] = {
    Diagnosis.DNS_FAILED: ("The camera's host name could not be resolved.", "Check the address, or use the camera's IP address."),
    Diagnosis.NETWORK_UNREACHABLE: ("Nothing answered at this address from the Visionary server.", "Check that the camera is on the same network as the server or gateway, and that the IP address is right."),
    Diagnosis.CAMERA_OFFLINE: ("The camera is not answering.", "Check the camera's power and network cable or Wi-Fi, then try again."),
    Diagnosis.NOT_ALLOWED_NETWORK: ("This address is on a private network Visionary is not allowed to reach.", "Add the network to CAMERA_PRIVATE_NETWORKS, or connect the camera through a Visionary Gateway on its network."),
    Diagnosis.ONVIF_DISABLED: ("The camera does not answer ONVIF requests.", "Enable ONVIF in the camera's web settings (often under Network > Integration), or add it with its RTSP address."),
    Diagnosis.RTSP_DISABLED: ("The camera does not accept RTSP connections on this port.", "Enable RTSP in the camera settings and check the RTSP port (usually 554)."),
    Diagnosis.AUTHENTICATION_FAILED: ("The camera is reachable but rejected the username or password.", "Check the credentials. Some cameras need a separate ONVIF user created in their web settings."),
    Diagnosis.LINK_EXPIRED: ("The stream link was refused. Its access token has probably expired (links with ?token=… are often time-limited).",
                             "Get a new link from the stream provider and paste it in the camera's Settings → Stream link."),
    Diagnosis.INVALID_CREDENTIALS: ("The camera needs a username and password.", "Enter the camera's username and password."),
    Diagnosis.STREAM_NOT_FOUND: ("The camera is reachable but this stream path does not exist.", "Use Find cameras / ONVIF to get the right path, or check the manufacturer's RTSP URL format."),
    Diagnosis.STREAM_TIMEOUT: ("The camera accepted the connection but sent no video in time.", "The camera may be overloaded or the network slow; try the sub stream or check the connection."),
    Diagnosis.UNSUPPORTED_CODEC: ("The stream uses a codec Visionary cannot decode.", "Set the camera stream to H.264 (or H.265) in its video settings."),
    Diagnosis.GATEWAY_OFFLINE: ("The Visionary Gateway for this camera's site is offline.", "Check that the gateway device is powered on and connected to the Internet."),
    Diagnosis.PROPRIETARY_PROTOCOL: ("This camera only speaks its manufacturer's own protocol (no ONVIF or RTSP found).", "Check whether the model can enable ONVIF/RTSP; otherwise it needs a manufacturer integration."),
    Diagnosis.PROVIDER_API_UNAVAILABLE: ("The manufacturer's cloud integration is not configured or not reachable.", "Ask an administrator to add the manufacturer's official API credentials."),
    Diagnosis.UNKNOWN_ERROR: ("An unexpected error occurred while connecting.", "Try again; if it persists, check the server logs (passwords are never logged)."),
}


@dataclass
class Check:
    name: str  # dns | reachable | onvif | rtsp | authentication | stream | codec | resolution | fps | latency | stability
    ok: bool | None  # None = skipped / not applicable
    detail: str = ""
    code: Diagnosis = Diagnosis.OK
    ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["code"] = self.code.value
        if not self.ok and self.code in GUIDANCE:
            d["explanation"], d["next_step"] = GUIDANCE[self.code]
        return d


@dataclass
class ProbeReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks) and any(c.ok for c in self.checks)

    @property
    def failure(self) -> Check | None:
        return next((c for c in self.checks if c.ok is False), None)

    def as_dict(self) -> dict[str, Any]:
        failure = self.failure
        return {"ok": self.ok, "checks": [c.as_dict() for c in self.checks],
                "diagnosis": failure.code.value if failure else "OK",
                "explanation": GUIDANCE.get(failure.code, ("", ""))[0] if failure else "Camera connected.",
                "next_step": GUIDANCE.get(failure.code, ("", ""))[1] if failure else ""}
