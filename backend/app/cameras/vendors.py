"""Manufacturer integrations (plugin registry).

Core camera code never contains vendor logic: a vendor registers a VendorProvider here and implements its
own CameraConnection. Only official, documented APIs/SDKs are used; proprietary protocols are never
reverse-engineered. When a vendor's API is not configured, probing reports PROVIDER_API_UNAVAILABLE.

Hikvision, Dahua and Uniview cameras normally work through the standard ONVIF / RTSP connections, so they
need no vendor plugin for live video. Vendor plugins are for cloud-only devices (e.g. Imou consumer cameras).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.cameras.connections import CameraConnection, ChannelInfo, ProbeResult, StreamProfile
from app.cameras.diagnostics import Check, Diagnosis, ProbeReport
from app.core.config import get_settings


class VendorProvider(ABC):
    key: str
    name: str
    docs: str

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def connection(self, spec: dict[str, Any], username: str | None, password: str | None) -> CameraConnection: ...

    def info(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "configured": self.configured(), "docs": self.docs}


class ImouConnection(CameraConnection):
    """Imou Open Platform (official cloud API, https://open.imoulife.com). A device bound to the Imou
    developer account is addressed by its serial number (deviceId) and channel; the API returns an HLS
    live address that Visionary ingests like any HLS stream.

    Not verified against a real Imou account in this build (no developer credentials available)."""

    kind = "vendor"
    BASE = "https://openapi.easy4ip.com/openapi"

    def __init__(self, spec: dict[str, Any], app_id: str | None, app_secret: str | None) -> None:
        super().__init__()
        self.device_id, self.channel = spec.get("device_id", ""), str(spec.get("channel", "0"))
        self.app_id, self.app_secret = app_id, app_secret

    def _system(self) -> dict[str, Any]:
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        sign = hashlib.md5(f"time:{ts},nonce:{nonce},appSecret:{self.app_secret}".encode()).hexdigest()
        return {"ver": "1.0", "appId": self.app_id, "sign": sign, "time": ts, "nonce": nonce}

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self.BASE}/{method}", json={"system": self._system(), "params": params, "id": uuid.uuid4().hex})
        result = (r.json() or {}).get("result", {})
        if str(result.get("code")) != "0":
            raise RuntimeError(result.get("msg") or f"Imou API error {result.get('code')}")
        return result.get("data") or {}

    async def probe(self, *, measure: bool = True) -> ProbeResult:
        report = ProbeReport()
        if not (self.app_id and self.app_secret):
            report.add(Check("provider", False, "Imou Open Platform credentials are not configured", Diagnosis.PROVIDER_API_UNAVAILABLE))
            return ProbeResult(report)
        try:
            token = (await self._call("accessToken", {})).get("accessToken")
            online = await self._call("deviceOnline", {"token": token, "deviceId": self.device_id})
            if str(online.get("onLine")) != "1":
                report.add(Check("reachable", False, "Imou reports the device offline", Diagnosis.CAMERA_OFFLINE))
                return ProbeResult(report)
            live = await self._call("bindDeviceLive", {"token": token, "deviceId": self.device_id, "channelId": self.channel, "streamId": 1})
        except Exception as exc:  # noqa: BLE001
            report.add(Check("provider", False, str(exc)[:160], Diagnosis.PROVIDER_API_UNAVAILABLE))
            return ProbeResult(report)
        streams = [s for s in (live.get("streams") or []) if s.get("hls")]
        if not streams:
            report.add(Check("stream", False, "Imou returned no HLS address", Diagnosis.STREAM_NOT_FOUND))
            return ProbeResult(report)
        report.add(Check("provider", True, "Imou Open Platform"))
        return ProbeResult(report, {"connection_type": "vendor", "vendor": "imou", "video": True, "remote_access": True},
                           [ChannelInfo(self.channel, f"Imou {self.device_id}", [StreamProfile("main", streams[0]["hls"])])])

    def spec(self) -> dict[str, Any]:
        return {"type": "vendor", "vendor": "imou", "device_id": self.device_id, "channel": self.channel}


class ImouProvider(VendorProvider):
    key, name, docs = "imou", "Imou (Open Platform)", "https://open.imoulife.com"

    def configured(self) -> bool:
        s = get_settings()
        return bool(s.imou_app_id and s.imou_app_secret)

    def connection(self, spec: dict[str, Any], username: str | None, password: str | None) -> CameraConnection:
        s = get_settings()
        return ImouConnection(spec, s.imou_app_id, s.imou_app_secret.get_secret_value() if s.imou_app_secret else None)


VENDORS: dict[str, VendorProvider] = {p.key: p for p in (ImouProvider(),)}


def get_vendor(key: str) -> VendorProvider:
    if key not in VENDORS:
        raise ValueError(f"No integration for {key!r}")
    return VENDORS[key]
