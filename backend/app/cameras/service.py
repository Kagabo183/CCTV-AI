"""Camera service: registration, credentials, media paths, status and health.

States: online · offline · connecting · auth_failed · stream_error · gateway_offline · disconnected.
The media server keeps pulling each camera and retries by itself; the health monitor reads its path state
every CAMERA_HEARTBEAT_SECONDS, measures bitrate, counts reconnects and, when a camera is down, diagnoses
why (with exponential backoff, capped at 5 minutes, so an offline camera is not hammered).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras import credentials as vault
from app.cameras import media_server, rtsp
from app.cameras.access import audit
from app.cameras.connections import ChannelInfo, ProbeResult, build_connection, stream_url
from app.cameras.diagnostics import Diagnosis, ProbeReport
from app.core.config import get_settings
from app.core.errors import ValidationFailed
from app.db.session import get_sessionmaker
from app.models import CameraCredential, Gateway, User, VideoSourceRecord
from app.video.sources.live import CAMERA_KINDS, LIVE_KINDS

logger = logging.getLogger(__name__)

KIND_FOR = {"rtsp": "camera_rtsp", "onvif": "camera_onvif", "hls": "camera_hls", "vendor": "camera_vendor"}
DEFAULT_AI_PROFILE = {"general": True, "people": True, "vehicles": True, "wildlife": False, "species": False, "tracking": True, "events": True, "record": True}
_health: dict[uuid.UUID, dict[str, Any]] = {}  # in-memory per-camera health (bitrate, failures, next check)
_monitor_task: asyncio.Task[None] | None = None


def is_camera(source: VideoSourceRecord) -> bool:
    return source.kind in CAMERA_KINDS


def check_network_allowed(url: str) -> None:
    """Cameras on private networks are only reachable when that network was explicitly allowed
    (CAMERA_PRIVATE_NETWORKS). Unresolvable hosts pass through so the probe can report DNS_FAILED."""
    import socket

    from app.cameras.diagnostics import GUIDANCE
    from app.video.url_safety import UrlPolicy

    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return
    policy = UrlPolicy(camera_networks=tuple(get_settings().camera_private_networks))
    if not all(policy.ip_allowed(ip) for ip in ips):
        explanation, step = GUIDANCE[Diagnosis.NOT_ALLOWED_NETWORK]
        raise ValidationFailed(f"{explanation} {step}", code="not_allowed_network")
    import ipaddress

    if any(ipaddress.ip_address(ip).is_loopback for ip in ips):
        # loopback is only for local test cameras: never let a camera URL reach Visionary's own services
        s = get_settings()
        own = {8000, s.media_rtsp_port, s.media_webrtc_port, s.media_hls_port, s.media_api_port, s.media_playback_port}
        if parts.port in own:
            raise ValidationFailed("That address belongs to Visionary itself, not to a camera.", code="not_allowed_network")


def _secret_pair(row: CameraCredential) -> tuple[str | None, str | None]:
    secret = vault.decrypt(row.secret)
    return secret.get("username"), secret.get("password")


async def credentials_for(db: AsyncSession, source: VideoSourceRecord) -> tuple[str | None, str | None]:
    target = source.parent_id or source.id
    row = (await db.execute(select(CameraCredential).where(CameraCredential.video_source_id == target))).scalar_one_or_none()
    if row is None:
        return None, None
    return _secret_pair(row)


async def test_connection(spec: dict[str, Any], username: str | None, password: str | None, *, measure: bool = True) -> ProbeResult:
    if spec.get("type") in ("rtsp", "hls"):
        check_network_allowed(spec["url"])
    elif spec.get("type") in ("onvif", "nvr"):
        check_network_allowed(spec["xaddr"] if "://" in spec["xaddr"] else f"http://{spec['xaddr']}")
    return await build_connection(spec, username, password).probe(measure=measure)


def _stream_dicts(channel: ChannelInfo) -> list[dict[str, Any]]:
    return [s.as_dict() for s in channel.streams]


async def register_media(source: VideoSourceRecord, username: str | None, password: str | None) -> None:
    """Media server paths for a camera: main (recorded when the AI profile says so) and sub if present."""
    if source.kind not in LIVE_KINDS:
        return
    streams = {s["role"]: s for s in source.source_metadata.get("streams", [])}
    record = bool((source.ai_profile or {}).get("record", True))
    if source.gateway_id:  # a gateway pushes these paths; the server never connects to the camera
        for role in streams:
            await media_server.ensure_publisher_path(media_server.gateway_path(source.gateway_id, source.id, role), record=record and role == "main")
        return
    for role, s in streams.items():
        if role in ("main", "sub"):
            await media_server.ensure_path(media_server.camera_path(source.id, role), stream_url(s["uri"], username, password), record=record and role == "main")
    if needs_web_stream(source):
        base = "sub" if "sub" in streams else "main"
        await media_server.ensure_web_path(media_server.camera_path(source.id, "web"), media_server.camera_path(source.id, base), streams[base].get("height"))


def needs_web_stream(source: VideoSourceRecord) -> bool:
    """No H.264 stream at all (e.g. H.265-only cameras): browsers without H.265 WebRTC get a transcoded copy."""
    codecs = {s.get("codec") for s in source.source_metadata.get("streams", [])}
    return bool(codecs) and "H.264" not in codecs


async def unregister_media(source: VideoSourceRecord) -> None:
    for role in ("main", "sub", "low", "web"):
        await media_server.remove_path(media_server.camera_path(source.id, role))
        if source.gateway_id:
            await media_server.remove_path(media_server.gateway_path(source.gateway_id, source.id, role))


async def add_camera(db: AsyncSession, user: User, *, name: str, location: str | None, spec: dict[str, Any], username: str | None, password: str | None,
                     channels: list[str] | None = None, gateway_id: uuid.UUID | None = None, ai_profile: dict[str, Any] | None = None,
                     probe: ProbeResult | None = None) -> list[VideoSourceRecord]:
    """Probe (unless given), then register the camera, or an NVR and one camera per selected channel."""
    if gateway_id is None:
        probe = probe or await test_connection(spec, username, password, measure=False)
    if probe is None or not probe.ok:
        report = probe.report.as_dict() if probe else ProbeReport().as_dict()
        raise ValidationFailed(f"{report['explanation']} {report['next_step']}".strip(), code=report["diagnosis"].lower())
    profile = {**DEFAULT_AI_PROFILE, **(ai_profile or {})}
    now = datetime.now(UTC)
    created: list[VideoSourceRecord] = []
    common = {"camera": True, "connection": spec, "capabilities": probe.capabilities, "device": probe.device}
    selected = [c for c in probe.channels if not channels or c.key in channels]

    if spec.get("type") == "nvr":
        nvr = VideoSourceRecord(owner_id=user.id, name=name, location=location, kind="nvr", uri=spec["xaddr"], status="ready",
                                source_metadata={**common, "channels": [{"key": c.key, "name": c.name} for c in probe.channels]},
                                connection_state="online", last_seen_at=now, last_validated_at=now, gateway_id=gateway_id, ai_profile=profile)
        db.add(nvr)
        await db.flush()
        created.append(nvr)
        owner_for_secret = nvr
        for c in selected:
            main = c.stream("main")
            db.add(ch := VideoSourceRecord(owner_id=user.id, name=f"{name} · {c.name}", location=location, kind="nvr_channel", uri=main.uri if main else "",
                                           status="ready", parent_id=nvr.id, gateway_id=gateway_id, connection_state="connecting", last_validated_at=now,
                                           source_metadata={**common, "channel": c.key, "streams": _stream_dicts(c)}, ai_profile=profile))
            created.append(ch)
    else:
        c = selected[0]
        main = c.stream("main")
        source = VideoSourceRecord(owner_id=user.id, name=name, location=location, kind=KIND_FOR[spec["type"]], uri=main.uri if main else "",
                                   status="ready", gateway_id=gateway_id, connection_state="connecting", last_validated_at=now,
                                   source_metadata={**common, "channel": c.key, "streams": _stream_dicts(c)}, ai_profile=profile)
        db.add(source)
        created.append(source)
        owner_for_secret = source
    await db.flush()
    if username:
        db.add(CameraCredential(video_source_id=owner_for_secret.id, secret=vault.encrypt({"username": username, "password": password or ""})))
    await audit(db, user, "camera.add", "camera", owner_for_secret.id, type=spec.get("type"), channels=len(created) - (1 if spec.get("type") == "nvr" else 0), via_gateway=bool(gateway_id))
    await db.commit()
    for s in created:
        await register_media(s, username, password)
    from app.cameras import live

    from app.cameras import device_events

    for s in created:
        live.start(s.id)
        if device_events.wanted(s):
            device_events.start(s.id)
    return created


async def remove_camera(db: AsyncSession, user: User, source: VideoSourceRecord) -> None:
    from app.cameras import live

    children = (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.parent_id == source.id))).scalars().all()
    for s in [*children, source]:
        live.stop(s.id)
        from app.cameras import device_events

        device_events.stop(s.id)
        await unregister_media(s)
        _health.pop(s.id, None)
    await audit(db, user, "camera.delete", "camera", source.id, name=source.name)


async def restore_all() -> int:
    """Re-register every camera with the media server and restart live AI (after a restart)."""
    from app.cameras import live

    n = 0
    async with get_sessionmaker()() as db:
        cams = (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.kind.in_(sorted(LIVE_KINDS))))).scalars().all()
        for source in cams:
            try:
                user, password = await credentials_for(db, source)
                await register_media(source, user, password)
                if source.connection_state not in ("disconnected",):
                    live.start(source.id)
                    from app.cameras import device_events

                    if device_events.wanted(source):
                        device_events.start(source.id)
                n += 1
            except Exception:  # noqa: BLE001
                logger.exception("Could not restore camera %s", source.id)
    return n


# -------------------------------------------------------------------------------------------- health
def forget_health(source_id: uuid.UUID) -> None:
    """Start the camera's health from scratch (e.g. after its link was replaced): no backoff left over."""
    _health.pop(source_id, None)


def health(source_id: uuid.UUID) -> dict[str, Any]:
    return dict(_health.get(source_id, {}))


STATE_FOR = {
    Diagnosis.CAMERA_OFFLINE: "offline", Diagnosis.NETWORK_UNREACHABLE: "offline", Diagnosis.RTSP_DISABLED: "offline", Diagnosis.DNS_FAILED: "offline",
    Diagnosis.AUTHENTICATION_FAILED: "auth_failed", Diagnosis.INVALID_CREDENTIALS: "auth_failed", Diagnosis.LINK_EXPIRED: "auth_failed",
    Diagnosis.STREAM_NOT_FOUND: "stream_error", Diagnosis.STREAM_TIMEOUT: "stream_error", Diagnosis.UNSUPPORTED_CODEC: "stream_error",
}


async def check_camera(source: VideoSourceRecord, gateways: dict[uuid.UUID, Gateway], creds: dict[uuid.UUID, tuple[str | None, str | None]]) -> None:
    """No database access in here: many cameras are checked concurrently."""
    if source.connection_state == "disconnected":
        return
    h = _health.setdefault(source.id, {"failures": 0, "reconnects": 0, "next_check": 0.0, "bytes": 0, "at": time.monotonic()})
    path = (media_server.gateway_path(source.gateway_id, source.id) if source.gateway_id else media_server.camera_path(source.id))
    st = await media_server.path_state(path)
    now = time.monotonic()
    previous = source.connection_state
    if st and st["ready"]:
        dt = max(now - h["at"], 1e-3)
        h["bitrate_kbps"] = round(max(0, st["bytes_received"] - h["bytes"]) * 8 / dt / 1000, 1) if h["bytes"] else None
        h.update(bytes=st["bytes_received"], at=now, failures=0, next_check=0.0, codecs=st["tracks"], viewers=st["readers"], diagnosis="OK")
        if previous != "online":
            if previous in ("offline", "stream_error", "auth_failed", "gateway_offline"):
                h["reconnects"] += 1
                _history(source, "last_reconnect")
            h["online_since"] = datetime.now(UTC).isoformat()
        source.connection_state, source.last_seen_at = "online", datetime.now(UTC)
        return
    if source.gateway_id:
        gw = gateways.get(source.gateway_id)
        if gw is None or gw.status != "online":
            source.connection_state = "gateway_offline"
            h["diagnosis"] = Diagnosis.GATEWAY_OFFLINE.value
            return
    if previous == "connecting" and st is not None and now - h.get("created", now) < 30:
        h.setdefault("created", now)
        return  # just added: give the media server time to connect
    if now < h["next_check"]:
        return
    h["failures"] += 1
    h["next_check"] = now + min(300.0, get_settings().camera_heartbeat_seconds * 2 ** min(h["failures"], 5))
    if source.gateway_id:
        source.connection_state, h["diagnosis"] = "stream_error", Diagnosis.STREAM_TIMEOUT.value
        return
    streams = {s["role"]: s for s in source.source_metadata.get("streams", [])}
    main = streams.get("main")
    if not main or not main["uri"].startswith(("rtsp://", "rtsps://")):
        source.connection_state = "stream_error"
        return
    user, password = creds.get(source.parent_id or source.id, (None, None))
    report = ProbeReport()
    ok = await rtsp.describe(main["uri"], user, password, report, timeout=4.0)
    failure = report.failure
    code = failure.code if failure else (Diagnosis.STREAM_TIMEOUT if not ok else Diagnosis.OK)
    h["diagnosis"] = code.value
    source.connection_state = "connecting" if ok else STATE_FOR.get(code, "offline")
    if ok:  # reachable again but the media server lost it: re-register to force a fresh pull
        await register_media(source, user, password)


def _history(source: VideoSourceRecord, key: str) -> None:
    """Persisted connection history (survives restarts): reconnect_count, last_disconnect, last_reconnect."""
    hist = dict(source.source_metadata.get("connection_history") or {})
    hist[key] = datetime.now(UTC).isoformat()
    if key == "last_reconnect":
        hist["reconnect_count"] = int(hist.get("reconnect_count", 0)) + 1
    source.source_metadata = {**source.source_metadata, "connection_history": hist}


def _went_down(source: VideoSourceRecord, previous: str | None) -> None:
    if previous == "online" and source.connection_state != "online":
        _history(source, "last_disconnect")


async def monitor_once() -> None:
    async with get_sessionmaker()() as db:
        cams = (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.kind.in_(sorted(LIVE_KINDS))))).scalars().all()
        gateways = {g.id: g for g in (await db.execute(select(Gateway))).scalars().all()}
        creds = {row.video_source_id: _secret_pair(row) for row in (await db.execute(select(CameraCredential))).scalars().all()}
        before = {c.id: c.connection_state for c in cams}
        for result in await asyncio.gather(*(check_camera(c, gateways, creds) for c in cams), return_exceptions=True):
            if isinstance(result, Exception):
                logger.warning("camera check failed: %s", result)
        for c in cams:
            _went_down(c, before[c.id])
        # NVR parents: online if any channel is
        for nvr in (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.kind == "nvr"))).scalars().all():
            states = [c.connection_state for c in cams if c.parent_id == nvr.id]
            nvr.connection_state = "online" if "online" in states else (states[0] if states else nvr.connection_state)
        await db.commit()


async def _monitor_loop() -> None:
    while True:
        try:
            await monitor_once()
        except Exception:  # noqa: BLE001
            logger.exception("camera health monitor")
        # media server state is a cheap local read, so look often; cameras themselves are only
        # contacted on failure, with the per-camera exponential backoff in check_camera
        await asyncio.sleep(min(5.0, get_settings().camera_heartbeat_seconds))


def start_monitor() -> None:
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())


def stop_monitor() -> None:
    if _monitor_task is not None:
        _monitor_task.cancel()
