"""Live cameras: add (test first), status, live view tokens, snapshots, recordings, events, sharing, gateways.

Nothing here ever returns a camera password, a gateway secret or an RTSP URL with credentials. Live video is
served by the media server; the browser gets a short-lived token scoped to one camera path.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response, WebSocket, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.cameras import access, gateway_hub, live, media_server, service
from app.cameras.connections import ProbeResult
from app.cameras.diagnostics import Diagnosis
from app.cameras.credentials import redact, split_credentials
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationFailed
from app.models import CameraShare, Gateway, User, VideoEvent, VideoSourceRecord

router = APIRouter(tags=["cameras"])


# ------------------------------------------------------------------------------------------ schemas
class ConnectionSpec(BaseModel):
    type: Literal["rtsp", "onvif", "nvr", "hls", "vendor"]
    url: str | None = Field(None, max_length=2048)  # rtsp / hls (credentials in the URL are moved to the vault)
    sub_url: str | None = Field(None, max_length=2048)
    xaddr: str | None = Field(None, max_length=512)  # onvif / nvr: host[:port] or device service URL
    vendor: str | None = Field(None, max_length=32)
    device_id: str | None = Field(None, max_length=128)
    channel: int | None = None

    def clean(self) -> tuple[dict[str, Any], str | None, str | None]:
        spec = self.model_dump(exclude_none=True)
        user = pw = None
        for key in ("url", "sub_url"):
            if spec.get(key):
                spec[key], u, p = split_credentials(spec[key].strip())
                user, pw = user or u, pw or p
        if self.type in ("rtsp", "hls") and not spec.get("url"):
            raise ValidationFailed("Enter the stream URL.")
        if self.type in ("onvif", "nvr") and not spec.get("xaddr"):
            raise ValidationFailed("Enter the camera or NVR address (IP or host name).")
        return spec, user, pw


class TestIn(BaseModel):
    connection: ConnectionSpec
    username: str | None = Field(None, max_length=128)
    password: str | None = Field(None, max_length=256)
    gateway_id: uuid.UUID | None = None


class CameraIn(TestIn):
    name: str = Field(..., min_length=1, max_length=200)
    location: str | None = Field(None, max_length=200)
    channels: list[str] | None = None  # NVR: which channels become cameras (default: all)
    ai_profile: dict[str, bool] | None = None


class CameraPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    location: str | None = Field(None, max_length=200)
    ai_profile: dict[str, bool] | None = None
    username: str | None = Field(None, max_length=128)
    password: str | None = Field(None, max_length=256)
    # a new stream link for cameras added from a link (e.g. when the provider's ?token= expired)
    url: str | None = Field(None, max_length=2048)
    sub_url: str | None = Field(None, max_length=2048)


class ShareIn(BaseModel):
    email: str = Field(..., max_length=320)
    role: Literal["admin", "operator", "viewer"]
    permissions: list[str] = []


class GatewayIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    location: str | None = Field(None, max_length=200)


class EnrollIn(BaseModel):
    token: str = Field(..., max_length=200)
    hostname: str | None = Field(None, max_length=100)
    version: str | None = Field(None, max_length=40)
    networks: list[str] = []


class DiscoverIn(BaseModel):
    gateway_id: uuid.UUID | None = None
    timeout: float = Field(3.0, ge=1.0, le=10.0)


# ------------------------------------------------------------------------------------------ helpers
def _iso(dt: datetime | None) -> str | None:
    """SQLite returns naive datetimes (stored as UTC): always send an explicit offset to browsers."""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def _streams(source: VideoSourceRecord) -> list[dict[str, Any]]:
    return [{**s, "uri": redact(s.get("uri", ""))} for s in source.source_metadata.get("streams", [])]


async def camera_out(db: DbSession, user: User, source: VideoSourceRecord, *, detail: bool = False) -> dict[str, Any]:
    role, perms = await access.role_for(db, user, source)
    meta = source.source_metadata or {}
    conn = {k: (redact(v) if isinstance(v, str) else v) for k, v in (meta.get("connection") or {}).items()}
    out: dict[str, Any] = {
        "id": str(source.id), "name": source.name, "location": source.location, "kind": source.kind, "parent_id": str(source.parent_id) if source.parent_id else None,
        "gateway_id": str(source.gateway_id) if source.gateway_id else None, "state": source.connection_state or "connecting",
        "last_seen_at": _iso(source.last_seen_at) if source.last_seen_at else None, "role": role, "permissions": sorted(perms),
        "connection": {"type": conn.get("type"), "address": conn.get("url") or conn.get("xaddr"), "vendor": conn.get("vendor")},
        # device service addresses (xaddr, PTZ/event URLs) stay server-side
        "device": {k: v for k, v in (meta.get("device") or {}).items() if k in ("manufacturer", "model", "firmware", "serial", "onvif_profiles", "media_version")},
        "capabilities": capability_profile(source), "streams": _streams(source),
        "channel": meta.get("channel"), "ai_profile": source.ai_profile or {}, "health": {**service.health(source.id), **(meta.get("connection_history") or {})},
        "live": live.state(source.id), "created_at": _iso(source.created_at),
    }
    if detail and source.kind == "nvr":
        kids = (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.parent_id == source.id))).scalars().all()
        out["channels"] = [{"id": str(k.id), "name": k.name, "state": k.connection_state, "channel": k.source_metadata.get("channel")} for k in kids]
    return out


def capability_profile(source: VideoSourceRecord) -> dict[str, Any]:
    """What this camera can do IN VISIONARY (the UI shows only these controls). Unknown = false."""
    meta = source.source_metadata or {}
    caps = meta.get("capabilities") or {}
    services = (meta.get("device") or {}).get("services") or {}
    streams = meta.get("streams", [])
    codecs = {s.get("codec") for s in streams}
    record = bool((source.ai_profile or {}).get("record", True))
    return {
        "video": bool(streams) or bool(caps.get("video")),
        "audio": bool(caps.get("audio")) or any(s.get("audio") for s in streams),
        "talk": False,  # two-way audio (ONVIF backchannel) is not implemented
        "ptz": bool(services.get("ptz")) and source.kind == "camera_onvif" and not source.gateway_id,
        "recording": record,
        "playback": record,
        "events": bool(services.get("events")),
        "snapshot": True,
        "h264": "H.264" in codecs or bool(caps.get("h264")),
        "h265": "H.265" in codecs or bool(caps.get("h265")),
        "substream": any(s.get("role") == "sub" for s in streams),
        "ai": bool((source.ai_profile or {}).get("general", True) or (source.ai_profile or {}).get("wildlife")),
        "onvif_profiles": caps.get("onvif_profiles") or [],
    }


def _event_out(e: VideoEvent, recording: bool) -> dict[str, Any]:
    meta = e.event_metadata or {}
    at = e.occurred_at.replace(tzinfo=UTC) if e.occurred_at and e.occurred_at.tzinfo is None else e.occurred_at
    return {"id": str(e.id), "camera_id": str(e.video_source_id), "type": e.event_type, "object": e.object_class, "track_id": e.track_id, "zone": e.zone,
            "description": e.description, "confidence": e.confidence, "evidence": e.evidence_level, "detector": e.detector,
            "occurred_at": _iso(at), "bbox": meta.get("bbox"), "species": meta.get("species"), "species_candidate": meta.get("species_candidate"),
            # evidence: the recording around the moment (5 s before to 15 s after), when the camera records
            "clip": {"start": _iso(at - timedelta(seconds=5)), "duration": 20} if recording and at else None}


def _path(source: VideoSourceRecord, role: str) -> str:
    roles = {s["role"] for s in source.source_metadata.get("streams", [])}
    role = role if role in roles else "main"
    return media_server.gateway_path(source.gateway_id, source.id, role) if source.gateway_id else media_server.camera_path(source.id, role)


async def _probe(spec: dict[str, Any], username: str | None, password: str | None, gateway_id: uuid.UUID | None, db: DbSession, user: User) -> ProbeResult:
    if gateway_id is None:
        return await service.test_connection(spec, username, password)
    gw = await db.get(Gateway, gateway_id)
    if gw is None or gw.owner_id != user.id:
        raise NotFoundError("Gateway not found")
    result = await gateway_hub.command(gateway_id, "probe", {"spec": spec, "username": username, "password": password}, timeout=60)
    return ProbeResult.from_dict(result)


# ------------------------------------------------------------------------------------------ cameras
@router.get("/cameras")
async def list_cameras(user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    shared = select(CameraShare.video_source_id).where(CameraShare.user_id == user.id)
    rows = (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.kind.in_(sorted(service.CAMERA_KINDS)),
                                                             (VideoSourceRecord.owner_id == user.id) | VideoSourceRecord.id.in_(shared)
                                                             | VideoSourceRecord.parent_id.in_(shared)).order_by(VideoSourceRecord.created_at))).scalars().all()
    return [await camera_out(db, user, s) for s in rows]


@router.post("/cameras/test-connection")
async def test_connection(body: TestIn, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Step 'test' of the Add-camera wizard: every check with a plain-language diagnosis. Nothing is saved."""
    spec, u, p = body.connection.clean()
    result = await _probe(spec, body.username or u, body.password or p, body.gateway_id, db, user)
    return result.as_dict()


@router.post("/cameras/discover")
async def discover(body: DiscoverIn, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """ONVIF WS-Discovery, only on networks the operator authorised (CAMERA_PRIVATE_NETWORKS) or via their gateway."""
    if body.gateway_id is not None:
        gw = await db.get(Gateway, body.gateway_id)
        if gw is None or gw.owner_id != user.id:
            raise NotFoundError("Gateway not found")
        result = await gateway_hub.command(body.gateway_id, "discover", {"timeout": body.timeout}, timeout=body.timeout + 10)
        from app.cameras.onvif import Discovered

        found = [Discovered(d.get("id", ""), d.get("xaddrs", []), d.get("scopes", []), d.get("ip", "")) for d in result.get("devices", [])]
        return {"devices": await _describe_found(found, user, db), "networks": result.get("networks", []), "via_gateway": True}
    from app.cameras import onvif
    from app.video.url_safety import UrlPolicy

    policy = UrlPolicy(camera_networks=tuple(get_settings().camera_private_networks))
    interfaces = [ip for ip in onvif.local_ipv4() if policy.in_camera_network(ip)]
    if not interfaces:
        return {"devices": [], "message": "No authorised camera network. Add your camera network to CAMERA_PRIVATE_NETWORKS, or use a gateway."}
    found = await asyncio.to_thread(onvif.discover, body.timeout, interfaces)
    return {"devices": await _describe_found([d for d in found if policy.in_camera_network(d.ip)], user, db), "networks": interfaces}


async def _describe_found(found: list[Any], user: User, db: DbSession) -> list[dict[str, Any]]:
    """What WS-Discovery tells without logging in: name/model scopes, ONVIF profiles, and whether it is already added."""
    import re as _re
    from urllib.parse import urlsplit

    mine = (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.owner_id == user.id, VideoSourceRecord.kind.in_(["camera_onvif", "nvr"])))).scalars().all()
    added = {urlsplit((m.source_metadata.get("device") or {}).get("xaddr") or "").netloc for m in mine}
    out = []
    for d in found:
        scope = lambda key: next((s.rsplit(f"/{key}/", 1)[1].replace("%20", " ") for s in d.scopes if f"/{key}/" in s), None)  # noqa: E731
        profiles = sorted({m.group(1) for s in d.scopes if (m := _re.search(r"/Profile/([A-Z])\b", s))} | ({"S"} if any(s.endswith("/Profile/Streaming") for s in d.scopes) else set()))
        netlocs = {urlsplit(x).netloc for x in d.xaddrs}
        out.append({"name": scope("name") or d.name, "hardware": scope("hardware"), "location": scope("location"), "ip": d.ip, "xaddrs": d.xaddrs, "id": d.address,
                    "onvif": True, "profiles": profiles, "profile_t": "T" in profiles, "already_added": bool(netlocs & added),
                    "kind_hint": "nvr" if _re.search(r"nvr|dvr|recorder", f"{scope('name')} {scope('hardware')}", _re.I) else "camera"})
    return out


@router.post("/cameras", status_code=status.HTTP_201_CREATED)
async def add_camera(body: CameraIn, user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    spec, u, p = body.connection.clean()
    username, password = body.username or u, body.password or p
    probe = await _probe(spec, username, password, body.gateway_id, db, user)
    created = await service.add_camera(db, user, name=body.name, location=body.location, spec=spec, username=username, password=password,
                                       channels=body.channels, gateway_id=body.gateway_id, ai_profile=body.ai_profile, probe=probe)
    if body.gateway_id is not None:
        for cam in created:
            if cam.kind != "nvr":
                await gateway_hub.push_streams(body.gateway_id, cam.id)
    return [await camera_out(db, user, c) for c in created]


@router.get("/cameras/{camera_id}")
async def get_camera(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    return await camera_out(db, user, await access.camera_for(db, user, camera_id, "live_view"), detail=True)


@router.patch("/cameras/{camera_id}")
async def update_camera(camera_id: uuid.UUID, body: CameraPatch, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    source = await access.camera_for(db, user, camera_id, "camera_settings")
    if body.name is not None:
        source.name = body.name
    if body.location is not None:
        source.location = body.location or None
    restart = False
    if body.ai_profile is not None:
        source.ai_profile = {**(source.ai_profile or {}), **body.ai_profile}
        restart = True
    if body.url:
        await _replace_link(db, user, source, body)
        return await camera_out(db, user, source)
    if body.username is not None or body.password is not None:
        from app.cameras import credentials as vault
        from app.models import CameraCredential

        target = source.parent_id or source.id
        row = (await db.execute(select(CameraCredential).where(CameraCredential.video_source_id == target))).scalar_one_or_none()
        old_user, old_pw = await service.credentials_for(db, source)
        secret = vault.encrypt({"username": body.username if body.username is not None else old_user, "password": body.password if body.password is not None else old_pw})
        if row is None:
            db.add(CameraCredential(video_source_id=target, secret=secret))
        else:
            row.secret = secret
        await access.audit(db, user, "camera.credentials", "camera", target)
        restart = True
    await db.commit()
    if restart:
        u, p = await service.credentials_for(db, source)
        await service.register_media(source, u, p)
        live.start(source.id)
    return await camera_out(db, user, source)


async def _replace_link(db: DbSession, user: User, source: VideoSourceRecord, body: CameraPatch) -> None:
    """Test the new link first; only a link that works replaces the old one (same camera, history and settings)."""
    from app.cameras import credentials as vault
    from app.models import CameraCredential

    kind = {"camera_rtsp": "rtsp", "camera_hls": "hls"}.get(source.kind)
    if kind is None:
        raise ValidationFailed("Only cameras added from a stream link can have their link replaced. For ONVIF cameras and recorders, update the login instead.")
    spec, u, p = ConnectionSpec(type=kind, url=body.url, sub_url=body.sub_url).clean()
    from app.cameras.rtsp import _has_access_token

    old_user, old_pw = await service.credentials_for(db, source)
    if _has_access_token(spec["url"]) and not (body.username or u):
        old_user = old_pw = None  # a link with its own token authenticates by itself: never mix in an old login
    username, password = body.username or u or old_user, body.password or p or old_pw
    probe = await _probe(spec, username, password, source.gateway_id, db, user)
    if not probe.ok:
        report = probe.report.as_dict()
        raise ValidationFailed(f"The new link does not work: {report['explanation']} {report['next_step']}".strip(), code=report["diagnosis"].lower())
    live.stop(source.id)
    await service.unregister_media(source)
    main = probe.channels[0].stream("main")
    source.uri = main.uri if main else spec["url"]
    source.source_metadata = {**source.source_metadata, "connection": spec, "capabilities": probe.capabilities,
                              "streams": [st.as_dict() for st in probe.channels[0].streams]}
    source.connection_state = "connecting"
    row = (await db.execute(select(CameraCredential).where(CameraCredential.video_source_id == source.id))).scalar_one_or_none()
    if username:
        secret = vault.encrypt({"username": username, "password": password or ""})
        if row is None:
            db.add(CameraCredential(video_source_id=source.id, secret=secret))
        else:
            row.secret = secret
    elif row is not None:
        await db.delete(row)  # the new link needs no login: do not keep sending the old one
    await access.audit(db, user, "camera.relink", "camera", source.id)
    await db.commit()
    service.forget_health(source.id)
    await service.register_media(source, username, password)
    if source.gateway_id:
        await gateway_hub.push_streams(source.gateway_id, source.id)
    live.start(source.id)


@router.delete("/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    source = await access.camera_for(db, user, camera_id, "delete")
    await service.remove_camera(db, user, source)
    await db.delete(source)
    await db.commit()


@router.post("/cameras/{camera_id}/connect")
async def connect(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    source = await access.camera_for(db, user, camera_id, "camera_settings")
    source.connection_state = "connecting"
    await db.commit()
    u, p = await service.credentials_for(db, source)
    await service.register_media(source, u, p)
    if source.gateway_id:
        await gateway_hub.push_streams(source.gateway_id, source.id)
    live.start(source.id)
    return await camera_out(db, user, source)


@router.post("/cameras/{camera_id}/disconnect")
async def disconnect(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    source = await access.camera_for(db, user, camera_id, "camera_settings")
    live.stop(source.id)
    await service.unregister_media(source)
    if source.gateway_id and gateway_hub.online(source.gateway_id):
        await gateway_hub.command(source.gateway_id, "stop_stream", {"camera_id": source.id.hex})
    source.connection_state = "disconnected"
    await db.commit()
    return await camera_out(db, user, source)


@router.get("/cameras/{camera_id}/status")
async def camera_status(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    source = await access.camera_for(db, user, camera_id, "live_view")
    st = await media_server.path_state(_path(source, "main"))
    return {"state": source.connection_state, "health": service.health(source.id), "media": st, "live_ai": live.state(source.id),
            "last_seen_at": _iso(source.last_seen_at) if source.last_seen_at else None}


@router.get("/cameras/{camera_id}/live")
async def live_view(camera_id: uuid.UUID, user: CurrentUser, db: DbSession, quality: Literal["main", "sub", "auto"] = "auto",
                    h265: bool = Query(True, description="false when the browser cannot decode H.265 over WebRTC")) -> dict[str, Any]:
    """A short-lived (5 min) token for one camera path. The token only opens the session; it is not reused."""
    source = await access.camera_for(db, user, camera_id, "live_view")
    if source.kind == "nvr":
        raise ValidationFailed("Choose one of the NVR's channels to watch.")
    roles = [s["role"] for s in source.source_metadata.get("streams", [])]
    role = quality if quality in roles else ("sub" if quality == "auto" and "sub" in roles else "main")
    codec = next((s.get("codec") for s in source.source_metadata.get("streams", []) if s["role"] == role), None)
    transcoded = not h265 and codec == "H.265" and service.needs_web_stream(source) and not source.gateway_id
    path = media_server.camera_path(source.id, "web") if transcoded else _path(source, role)
    token = media_server.stream_token(user.id, path, ("read",))
    await access.audit(db, user, "camera.live_view", "camera", source.id, stream=role)
    await db.commit()
    return {"whep_url": f"/media/webrtc/{path}/whep?token={token}", "hls_url": f"/media/hls/{path}/index.m3u8?token={token}",
            "message": None if source.connection_state in (None, "online", "connecting") else _why_no_video(source),
            "stream": "web" if transcoded else role, "codec": "H.264" if transcoded else codec, "transcoded": transcoded, "available_streams": roles, "state": source.connection_state, "expires_in": int(media_server.STREAM_TOKEN_TTL.total_seconds()),
            "audio": bool((source.source_metadata.get("capabilities") or {}).get("audio")), "ice_servers": _public_ice()}


def _why_no_video(source: VideoSourceRecord) -> str:
    """One plain sentence for the player: why there is no video and what to do."""
    from app.cameras.diagnostics import GUIDANCE

    code = service.health(source.id).get("diagnosis")
    try:
        explanation, step = GUIDANCE[Diagnosis(code)]
        return f"{explanation} {step}".strip()
    except (ValueError, KeyError):
        return {"auth_failed": "Visionary cannot log in to this camera. Check its login or link in Settings.",
                "gateway_offline": "The gateway at this camera's site is offline.",
                "disconnected": "This camera is paused. Connect it again in its Connection tab.",
                "offline": "The camera does not answer. Check its power and network.",
                "stream_error": "The camera answers but sends no video."}.get(source.connection_state or "", "The camera is not sending video right now.")


def _public_ice() -> list[dict[str, Any]]:
    """STUN/TURN for the browser. TURN credentials here are expected to be short-lived ones from the TURN provider."""
    out = []
    for url in get_settings().media_ice_servers:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        entry: dict[str, Any] = {"urls": f"{parts.scheme}:{parts.hostname}" + (f":{parts.port}" if parts.port else "") + (f"?{parts.query}" if parts.query else "")}
        if parts.username:
            entry.update(username=parts.username, credential=parts.password or "")
        out.append(entry)
    return out


@router.get("/cameras/{camera_id}/snapshot")
async def snapshot(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    source = await access.camera_for(db, user, camera_id, "live_view")
    url = media_server.internal_rtsp_url(_path(source, "sub"))

    def grab() -> bytes | None:
        import cv2

        recent = live.last_frame(source.id)
        if recent is not None:  # the live AI already decodes this stream
            ok, jpg = cv2.imencode(".jpg", recent, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                return jpg.tobytes()

        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        try:
            # The first frames after joining a stream can decode black/grey (no keyframe yet): read on for
            # up to ~1.5 s and keep the last good frame.
            import time as _t

            frame, deadline = None, _t.monotonic() + 1.5
            while _t.monotonic() < deadline:
                ok, f = cap.read()
                if not ok:
                    break
                frame = f
                if float(f.std()) > 8.0 and _t.monotonic() > deadline - 1.0:
                    break
            if frame is None:
                return None
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return jpg.tobytes() if ok else None
        finally:
            cap.release()

    data = await asyncio.wait_for(asyncio.to_thread(grab), 20)
    if data is None:
        raise ValidationFailed("The camera is not sending video right now.", code="stream_timeout")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/cameras/{camera_id}/events")
async def camera_events(camera_id: uuid.UUID, user: CurrentUser, db: DbSession, minutes: int = Query(60, ge=1, le=60 * 24 * 30),
                        limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    source = await access.camera_for(db, user, camera_id, "playback")
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    ids = [source.id] + list((await db.execute(select(VideoSourceRecord.id).where(VideoSourceRecord.parent_id == source.id))).scalars().all())
    rows = (await db.execute(select(VideoEvent).where(VideoEvent.video_source_id.in_(ids), VideoEvent.occurred_at >= since)
                             .order_by(VideoEvent.occurred_at.desc()).limit(limit))).scalars().all()
    recording = capability_profile(source)["recording"]
    return [_event_out(e, recording) for e in rows]


@router.get("/cameras/{camera_id}/capabilities")
async def camera_capabilities(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    return capability_profile(await access.camera_for(db, user, camera_id, "live_view"))


@router.get("/cameras/{camera_id}/streams")
async def camera_streams(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """The camera's streams and which one each job uses (live preview, phones, AI, recording)."""
    from app.cameras.live import ai_stream_role

    source = await access.camera_for(db, user, camera_id, "live_view")
    streams = source.source_metadata.get("streams", [])
    roles = {s["role"] for s in streams}
    small = "sub" if "sub" in roles else "main"
    return {"streams": _streams(source), "usage": {"live_view": "main (High quality) or " + small + " (Auto / Data saver)", "mobile": small,
                                                   "ai": ai_stream_role(streams, get_settings().live_ai_stream), "recording": "main"},
            "browser_copy": service.needs_web_stream(source)}


class PtzIn(BaseModel):
    action: Literal["move", "stop", "preset"]
    pan: float = Field(0.0, ge=-1, le=1)
    tilt: float = Field(0.0, ge=-1, le=1)
    zoom: float = Field(0.0, ge=-1, le=1)
    preset: str | None = Field(None, max_length=64)


async def _ptz_client(db: DbSession, source: VideoSourceRecord) -> tuple[Any, str, str]:
    from app.cameras.onvif import OnvifClient

    if not capability_profile(source)["ptz"]:
        raise ValidationFailed("This camera has no pan/tilt/zoom control.", code="not_supported")
    device = source.source_metadata.get("device") or {}
    token = next((s.get("token") for s in source.source_metadata.get("streams", []) if s["role"] == "main"), None)
    if not token:
        raise ValidationFailed("The camera did not give a media profile for PTZ.", code="not_supported")
    u, p = await service.credentials_for(db, source)
    client = OnvifClient(device["xaddr"], u, p)
    await client.sync_clock()
    return client, device["services"]["ptz"], token


@router.get("/cameras/{camera_id}/ptz")
async def ptz_info(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    source = await access.camera_for(db, user, camera_id, "ptz")
    client, addr, token = await _ptz_client(db, source)
    from app.cameras.onvif import OnvifError

    try:
        return {"presets": await client.ptz_presets(addr, token), "position": await client.ptz_status(addr, token)}
    except OnvifError as exc:
        raise ValidationFailed(f"The camera refused the PTZ request: {exc.detail}", code="ptz_failed") from exc


@router.post("/cameras/{camera_id}/ptz")
async def ptz_control(camera_id: uuid.UUID, body: PtzIn, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Continuous move (the browser sends stop when the button is released), stop, or go to a preset."""
    from app.cameras.onvif import OnvifError

    source = await access.camera_for(db, user, camera_id, "ptz")
    client, addr, token = await _ptz_client(db, source)
    try:
        if body.action == "move":
            await client.ptz_move(addr, token, body.pan, body.tilt, body.zoom)
        elif body.action == "stop":
            await client.ptz_stop(addr, token)
        else:
            if not body.preset:
                raise ValidationFailed("Choose a preset.")
            await client.ptz_goto(addr, token, body.preset)
            await access.audit(db, user, "camera.ptz_preset", "camera", source.id, preset=body.preset)
            await db.commit()
    except OnvifError as exc:
        raise ValidationFailed(f"The camera refused the PTZ request: {exc.detail}", code="ptz_failed") from exc
    return {"ok": True}


@router.get("/cameras/{camera_id}/diagnostics")
async def diagnostics(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Re-test a connected camera now (with its stored login) and report every stage down to the live AI."""
    from app.cameras import device_events, onvif, rtsp
    from app.cameras.diagnostics import Check, ProbeReport

    source = await access.camera_for(db, user, camera_id, "live_view")
    if source.kind == "nvr":
        raise ValidationFailed("Run diagnostics on one of the recorder's channels.")
    u, p = await service.credentials_for(db, source)
    report = ProbeReport()
    device = source.source_metadata.get("device") or {}
    main = next((s for s in source.source_metadata.get("streams", []) if s["role"] == "main"), None)
    if source.gateway_id:
        gw_online = gateway_hub.online(source.gateway_id)
        report.add(Check("gateway", gw_online, "the site's Visionary Gateway is connected" if gw_online else "the gateway is offline",
                         Diagnosis.OK if gw_online else Diagnosis.GATEWAY_OFFLINE))
        if gw_online and main:
            probe = ProbeResult.from_dict(await gateway_hub.command(source.gateway_id, "probe", {"spec": {"type": "rtsp", "url": main["uri"]}, "username": u, "password": p}, timeout=60))
            report.checks += probe.report.checks
    else:
        if source.kind == "camera_onvif" and device.get("xaddr"):
            onvif_report, _ = await onvif.probe(device["xaddr"], u, p)
            report.checks += [c for c in onvif_report.checks if c.name in ("reachable", "onvif", "authentication")]
        if main and main["uri"].startswith(("rtsp://", "rtsps://")):
            stream_report, _info = await rtsp.probe(main["uri"], u, p, measure=True)
            report.checks += [c for c in stream_report.checks if not (source.kind == "camera_onvif" and c.name in ("reachable",))]
    caps = capability_profile(source)
    report.add(Check("audio", None if not caps["audio"] else True, "the camera sends audio" if caps["audio"] else "no audio track"))
    report.add(Check("ptz", None if not caps["ptz"] else True, "pan/tilt/zoom available" if caps["ptz"] else "no pan/tilt/zoom"))
    media = await media_server.path_state(_path(source, "main"))
    report.add(Check("media_server", bool(media and media["ready"]), "Visionary is receiving the video" if media and media["ready"] else "Visionary is not receiving video from this camera",
                     Diagnosis.OK if media and media["ready"] else Diagnosis.STREAM_TIMEOUT))
    st = live.state(source.id)
    ai_on = bool(st and st.get("status") == "running")
    report.add(Check("ai_processing", ai_on if caps["ai"] else None,
                     (f"live AI active: {st.get('ai_fps')} frames/s, {st.get('latency_ms')} ms per frame" if ai_on else f"live AI {st.get('status') if st else 'not running'}") if caps["ai"] else "AI is off for this camera",
                     Diagnosis.OK if ai_on or not caps["ai"] else Diagnosis.STREAM_TIMEOUT))
    if caps["events"]:
        ev = device_events.status(source.id)
        report.add(Check("camera_events", ev.get("state") == "subscribed", f"camera event subscription: {ev.get('state', 'not started')}, {ev.get('events', 0)} event(s) received"))
    return {**report.as_dict(), "health": {**service.health(source.id), **(source.source_metadata.get("connection_history") or {})}, "state": source.connection_state}


@router.post("/cameras/{camera_id}/refresh")
async def refresh_capabilities(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Ask the device again what it supports (after a firmware update or a settings change on the camera)."""
    source = await access.camera_for(db, user, camera_id, "camera_settings")
    target = await db.get(VideoSourceRecord, source.parent_id) if source.parent_id else source
    spec = (target.source_metadata or {}).get("connection") or {}
    u, p = await service.credentials_for(db, source)
    probe = await _probe(spec, u, p, source.gateway_id, db, user)
    if not probe.ok:
        report = probe.report.as_dict()
        raise ValidationFailed(f"{report['explanation']} {report['next_step']}".strip(), code=report["diagnosis"].lower())
    for row in {target, source}:
        row.source_metadata = {**row.source_metadata, "capabilities": probe.capabilities, "device": probe.device}
    await access.audit(db, user, "camera.refresh", "camera", source.id)
    await db.commit()
    from app.cameras import device_events

    if device_events.wanted(source):
        device_events.start(source.id)
    return await camera_out(db, user, source)


@router.get("/cameras-connectors")
async def connectors(user: CurrentUser) -> list[dict[str, Any]]:
    """How each kind of camera connects. Brands without an official open integration are reached through the standards they implement."""
    s = get_settings()
    return [
        {"id": "onvif", "name": "ONVIF cameras and NVRs", "status": "available", "how": "Profile T first (H.264/H.265, events), Profile S for older devices. PTZ and camera events when the device offers them."},
        {"id": "rtsp", "name": "RTSP stream link", "status": "available", "how": "Any camera that gives an rtsp:// address (H.264 or H.265)."},
        {"id": "nvr", "name": "NVR / DVR recorders", "status": "available", "how": "ONVIF recorders: every channel becomes a camera. Others: one RTSP link per channel."},
        {"id": "hls", "name": "HLS / HTTP stream", "status": "available", "how": "Internet video streams (.m3u8)."},
        {"id": "gateway", "name": "Visionary Gateway", "status": "available", "how": "Cameras on another site's private network, without port forwarding."},
        {"id": "imou", "name": "Imou (cloud)", "status": "untested" if s.imou_app_id else "needs_configuration",
         "how": "Official Imou Open Platform API. Needs an Imou developer app (IMOU_APP_ID / IMOU_APP_SECRET); not yet verified with a real account."},
        *[{"id": b.lower(), "name": b, "status": "via_standard", "how": "No separate connector: add it as an ONVIF camera (enable ONVIF in the camera's settings) or with its RTSP link."}
          for b in ("Hikvision", "Dahua", "Uniview", "Axis", "Reolink", "TP-Link Tapo")],
    ]


@router.get("/cameras-events")
async def recent_events(user: CurrentUser, db: DbSession, minutes: int = Query(60, ge=1, le=60 * 24 * 7), limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    """Recent events across every camera the user may see (dashboard feed)."""
    cams = [c for c in await list_cameras(user, db) if "playback" in c["permissions"]]
    if not cams:
        return []
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    rows = (await db.execute(select(VideoEvent).where(VideoEvent.video_source_id.in_([uuid.UUID(c["id"]) for c in cams]), VideoEvent.occurred_at >= since,
                                                      VideoEvent.event_type != "object_disappeared").order_by(VideoEvent.occurred_at.desc()).limit(limit))).scalars().all()
    rec = {c["id"]: c["capabilities"]["recording"] for c in cams}
    return [_event_out(e, rec.get(str(e.video_source_id), False)) for e in rows]


@router.get("/cameras/{camera_id}/recordings")
async def camera_recordings(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    source = await access.camera_for(db, user, camera_id, "playback")
    # MediaMTX also returns its own (internal) download URL per span: never passed to browsers
    spans = [{"start": s.get("start"), "duration": s.get("duration")} for s in await media_server.recordings(_path(source, "main"))]
    return {"recording": bool((source.ai_profile or {}).get("record", True)), "retention": get_settings().recording_retention, "spans": spans}


@router.get("/cameras/{camera_id}/playback")
async def playback(camera_id: uuid.UUID, user: CurrentUser, db: DbSession, start: datetime, duration: float = Query(60, ge=1, le=3600)) -> StreamingResponse:
    """Recorded video from `start` (MP4), streamed from the media server's playback API through the backend."""
    import httpx

    source = await access.camera_for(db, user, camera_id, "playback")
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    url = media_server.playback_url(_path(source, "main"), start, duration)
    client = httpx.AsyncClient(timeout=120, trust_env=False, auth=(media_server.INTERNAL_USER, media_server.internal_password()))
    r = await client.send(client.build_request("GET", url), stream=True)
    if r.status_code != 200:
        await r.aclose()
        await client.aclose()
        raise NotFoundError("No recording for that time.")

    async def body():  # type: ignore[no-untyped-def]
        try:
            async for chunk in r.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(body(), media_type="video/mp4", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------------------------------ sharing
@router.get("/cameras/{camera_id}/shares")
async def list_shares(camera_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    source = await access.camera_for(db, user, camera_id, "share")
    rows = (await db.execute(select(CameraShare, User).join(User, User.id == CameraShare.user_id).where(CameraShare.video_source_id == source.id))).all()
    return [{"id": str(s.id), "email": u.email, "role": s.role, "permissions": s.permissions} for s, u in rows]


@router.post("/cameras/{camera_id}/shares", status_code=status.HTTP_201_CREATED)
async def add_share(camera_id: uuid.UUID, body: ShareIn, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    source = await access.camera_for(db, user, camera_id, "share")
    if body.role == "admin" and source.owner_id != user.id:
        raise access.Forbidden("Only the owner can add admins")
    bad = set(body.permissions) - set(access.PERMISSIONS)
    if bad:
        raise ValidationFailed(f"Unknown permission(s): {', '.join(sorted(bad))}")
    target = (await db.execute(select(User).where(User.email == body.email.lower().strip()))).scalar_one_or_none()
    if target is None or target.id == source.owner_id:
        raise NotFoundError("No Visionary user with that email")
    share = (await db.execute(select(CameraShare).where(CameraShare.video_source_id == source.id, CameraShare.user_id == target.id))).scalar_one_or_none()
    if share is None:
        share = CameraShare(video_source_id=source.id, user_id=target.id, role=body.role, permissions=body.permissions)
        db.add(share)
    else:
        share.role, share.permissions = body.role, body.permissions
    await access.audit(db, user, "camera.share", "camera", source.id, with_user=str(target.id), role=body.role)
    await db.commit()
    return {"id": str(share.id), "email": target.email, "role": share.role, "permissions": share.permissions}


@router.delete("/cameras/{camera_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_share(camera_id: uuid.UUID, share_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    source = await access.camera_for(db, user, camera_id, "share")
    share = await db.get(CameraShare, share_id)
    if share is None or share.video_source_id != source.id:
        raise NotFoundError("Share not found")
    await db.delete(share)
    await access.audit(db, user, "camera.unshare", "camera", source.id, share=str(share_id))
    await db.commit()


@router.get("/cameras-overview")
async def overview(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Dashboard numbers + observability: states, live AI throughput, media server sessions."""
    cams = await list_cameras(user, db)
    states: dict[str, int] = {}
    for c in cams:
        if c["kind"] != "nvr":
            states[c["state"]] = states.get(c["state"], 0) + 1
    mine = {c["id"].replace("-", "") for c in cams}
    sessions = [s for s in await media_server.webrtc_sessions() if any(m in (s.get("path") or "") for m in mine)]
    return {"cameras": len([c for c in cams if c["kind"] != "nvr"]), "states": states, "media_server": await media_server.healthy(),
            "viewers": len(sessions), "bytes_sent": sum(int(s.get("bytesSent") or 0) for s in sessions),
            "live_ai": {k: v for k, v in live.workers().items() if k.replace("-", "") in mine}, "batching": live.batch_stats()}


# ------------------------------------------------------------------------------------------ media server auth hook
@router.post("/media/auth", include_in_schema=False)
async def media_auth(request: Request, db: DbSession) -> Response:
    """Called by the local media server for every read / publish / playback. Only accepted from localhost."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        return Response(status_code=403)
    payload = await request.json()
    ok = await access.authorize_media(db, payload)
    return Response(status_code=200 if ok else 401)


# ------------------------------------------------------------------------------------------ gateways
def _gateway_out(gw: Gateway) -> dict[str, Any]:
    meta = gw.gateway_metadata or {}
    return {"id": str(gw.id), "name": gw.name, "location": gw.location, "status": "online" if gateway_hub.online(gw.id) else gw.status,
            "version": gw.version, "host": meta.get("host"), "networks": meta.get("networks", []), "health": meta.get("health", {}),
            "last_heartbeat_at": _iso(gw.last_heartbeat_at) if gw.last_heartbeat_at else None, "created_at": _iso(gw.created_at)}


@router.get("/gateways")
async def list_gateways(user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Gateway).where(Gateway.owner_id == user.id, Gateway.status != "revoked").order_by(Gateway.created_at))).scalars().all()
    cams = (await db.execute(select(VideoSourceRecord.gateway_id).where(VideoSourceRecord.gateway_id.in_([g.id for g in rows]), VideoSourceRecord.kind != "nvr"))).scalars().all()
    return [{**_gateway_out(g), "camera_count": sum(1 for c in cams if c == g.id)} for g in rows]


@router.post("/gateways", status_code=status.HTTP_201_CREATED)
async def create_gateway(body: GatewayIn, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Creates a gateway and returns its one-time enrolment code (shown once, expires)."""
    gw, token = await gateway_hub.create_enrollment(db, user, body.name, body.location)
    return {**_gateway_out(gw), "enrollment_token": token, "expires_at": _iso(gw.enrollment_expires_at) if gw.enrollment_expires_at else None}


@router.post("/gateways/enroll")
async def enroll_gateway(body: EnrollIn, db: DbSession) -> dict[str, Any]:
    """Called by the gateway itself (no user session): trades the one-time code for its identity."""
    gw, secret = await gateway_hub.enroll(db, body.token, body.model_dump())
    s = get_settings()
    return {"gateway_id": str(gw.id), "secret": secret, "control_path": f"/api/gateways/{gw.id}/ws",
            "media": {"rtsp_port": s.media_rtsp_port, "path_prefix": f"gw/{gw.id.hex}/", "user": gw.id.hex}}


@router.get("/gateways/{gateway_id}")
async def get_gateway(gateway_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    gw = await db.get(Gateway, gateway_id)
    if gw is None or gw.owner_id != user.id:
        raise NotFoundError("Gateway not found")
    cams = (await db.execute(select(VideoSourceRecord).where(VideoSourceRecord.gateway_id == gw.id))).scalars().all()
    return {**_gateway_out(gw), "cameras": [{"id": str(c.id), "name": c.name, "state": c.connection_state} for c in cams]}


@router.delete("/gateways/{gateway_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_gateway(gateway_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    gw = await db.get(Gateway, gateway_id)
    if gw is None or gw.owner_id != user.id:
        raise NotFoundError("Gateway not found")
    gw.status, gw.secret_hash, gw.enrollment_token_hash = "revoked", None, None
    await access.audit(db, user, "gateway.revoke", "gateway", gw.id)
    await db.commit()
    await gateway_hub.disconnect(gw.id)


@router.websocket("/gateways/{gateway_id}/ws")
async def gateway_ws(ws: WebSocket, gateway_id: uuid.UUID) -> None:
    from app.db.session import get_sessionmaker

    auth = ws.headers.get("authorization", "")
    secret = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    async with get_sessionmaker()() as db:
        gw = await gateway_hub.authenticate(db, gateway_id, secret) if secret else None
    if gw is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    await gateway_hub.serve(ws, gw)
