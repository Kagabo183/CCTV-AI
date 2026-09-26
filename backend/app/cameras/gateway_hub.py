"""Visionary Gateway hub (cloud side).

A gateway runs on the customer's network, next to the cameras. It only makes OUTBOUND connections:

  1. enrolment   POST /api/gateways/enroll with a one-time token (shown once to the owner, stored hashed,
                 expires after GATEWAY_ENROLLMENT_TTL_MINUTES) -> gateway id + long-lived secret (stored hashed)
  2. control     WebSocket /api/gateways/{id}/ws, authenticated with the secret. Heartbeats come up; commands
                 go down: probe, discover, start_streams, stop_stream. Replies carry the command id.
  3. media       the gateway pushes each camera stream (ffmpeg -c copy, no transcoding) to the media server at
                 rtsp://<hub>:8554/gw/<gateway>/<camera>/<role>, authenticated as the gateway (publish only
                 under its own gw/<gateway>/ prefix; checked in access.authorize_media).

No inbound port is opened on the customer's router and RTSP never crosses the Internet unauthenticated.
Camera passwords go down the (TLS) control channel only when the gateway needs them to pull a stream.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras import media_server
from app.cameras.access import audit, secret_hash
from app.core.config import get_settings
from app.core.errors import AppError, ValidationFailed
from app.db.session import get_sessionmaker
from app.models import Gateway, User, VideoSourceRecord

logger = logging.getLogger(__name__)


class GatewayOffline(AppError):
    status_code = 409

    def __init__(self, message: str = "The gateway is offline. Check that the gateway device is powered on and has Internet access.") -> None:
        super().__init__(message, code="gateway_offline")


@dataclass
class Link:
    gateway_id: uuid.UUID
    ws: WebSocket
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    connected_at: float = 0.0


_links: dict[uuid.UUID, Link] = {}


def online(gateway_id: uuid.UUID) -> bool:
    return gateway_id in _links


async def create_enrollment(db: AsyncSession, user: User, name: str, location: str | None) -> tuple[Gateway, str]:
    token = "vge_" + secrets.token_urlsafe(24)
    gw = Gateway(owner_id=user.id, name=name, location=location, status="pending", enrollment_token_hash=secret_hash(token),
                 enrollment_expires_at=datetime.now(UTC) + timedelta(minutes=get_settings().gateway_enrollment_ttl_minutes))
    db.add(gw)
    await db.flush()
    await audit(db, user, "gateway.create", "gateway", gw.id, name=name)
    await db.commit()
    return gw, token


async def enroll(db: AsyncSession, token: str, info: dict[str, Any]) -> tuple[Gateway, str]:
    gw = (await db.execute(select(Gateway).where(Gateway.enrollment_token_hash == secret_hash(token)))).scalar_one_or_none()
    expires = gw.enrollment_expires_at if gw else None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if gw is None or gw.status != "pending" or expires is None or expires < datetime.now(UTC):
        raise ValidationFailed("This enrolment code is invalid or has expired. Create a new one in Gateways.", code="invalid_enrollment")
    secret = "vgs_" + secrets.token_urlsafe(32)
    gw.secret_hash = secret_hash(secret)
    gw.enrollment_token_hash = None  # one use only
    gw.status = "offline"
    gw.version = str(info.get("version") or "")[:40] or None
    gw.gateway_metadata = {**(gw.gateway_metadata or {}), "host": str(info.get("hostname") or "")[:100], "networks": [str(n)[:50] for n in info.get("networks", [])][:16],
                           "enrolled_at": datetime.now(UTC).isoformat()}
    await audit(db, None, "gateway.enroll", "gateway", gw.id)
    await db.commit()
    return gw, secret


async def authenticate(db: AsyncSession, gateway_id: uuid.UUID, secret: str) -> Gateway | None:
    gw = await db.get(Gateway, gateway_id)
    if gw is None or gw.status in ("revoked", "pending") or not gw.secret_hash:
        return None
    return gw if hmac.compare_digest(secret_hash(secret), gw.secret_hash) else None


async def command(gateway_id: uuid.UUID, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    link = _links.get(gateway_id)
    if link is None:
        raise GatewayOffline
    cid = uuid.uuid4().hex
    fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    link.pending[cid] = fut
    try:
        await link.ws.send_json({"id": cid, "op": op, "args": args or {}})
        reply = await asyncio.wait_for(fut, timeout)
    except TimeoutError as exc:
        raise GatewayOffline("The gateway did not answer in time.") from exc
    finally:
        link.pending.pop(cid, None)
    if reply.get("error"):
        raise ValidationFailed(str(reply["error"])[:300], code="gateway_error")
    return reply.get("result") or {}


async def stream_specs(db: AsyncSession, gateway_id: uuid.UUID, only: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """What the gateway must push: each camera stream with its (decrypted) source URL and target path."""
    from app.cameras.connections import stream_url
    from app.cameras.service import credentials_for
    from app.video.sources.live import LIVE_KINDS

    q = select(VideoSourceRecord).where(VideoSourceRecord.gateway_id == gateway_id, VideoSourceRecord.kind.in_(sorted(LIVE_KINDS)))
    if only is not None:
        q = q.where(VideoSourceRecord.id == only)
    out = []
    for cam in (await db.execute(q)).scalars().all():
        if cam.connection_state == "disconnected":
            continue
        user, password = await credentials_for(db, cam)
        for s in cam.source_metadata.get("streams", []):
            if s["role"] in ("main", "sub"):
                out.append({"camera_id": cam.id.hex, "role": s["role"], "source": stream_url(s["uri"], user, password),
                            "path": media_server.gateway_path(gateway_id, cam.id, s["role"])})
    return out


async def push_streams(gateway_id: uuid.UUID, only: uuid.UUID | None = None) -> None:
    async with get_sessionmaker()() as db:
        specs = await stream_specs(db, gateway_id, only)
    if specs:
        await command(gateway_id, "start_streams", {"streams": specs})


async def serve(ws: WebSocket, gw: Gateway) -> None:
    """Run one gateway's control connection until it closes."""
    import time

    old = _links.pop(gw.id, None)
    if old is not None:
        await old.ws.close(code=4000)  # a newer connection replaces the old one
    link = _links[gw.id] = Link(gw.id, ws, connected_at=time.time())
    s = get_settings()
    await ws.send_json({"op": "hello", "args": {"gateway_id": gw.id.hex, "heartbeat_seconds": s.camera_heartbeat_seconds,
                                                "media": {"rtsp_port": s.media_rtsp_port, "path_prefix": f"gw/{gw.id.hex}/"}}})
    await _set_status(gw.id, "online")
    logger.info("Gateway %s connected", gw.id)
    resync = asyncio.create_task(push_streams(gw.id))
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("reply_to"):
                fut = link.pending.get(msg["reply_to"])
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif msg.get("op") == "heartbeat":
                await _set_status(gw.id, "online", health=msg.get("args") or {})
    except Exception:  # noqa: BLE001 - disconnect or bad frame: the gateway reconnects by itself
        pass
    finally:
        resync.cancel()
        if _links.get(gw.id) is link:
            _links.pop(gw.id, None)
            await _set_status(gw.id, "offline")
        for fut in link.pending.values():
            if not fut.done():
                fut.set_result({"error": "Gateway disconnected"})
        logger.info("Gateway %s disconnected", gw.id)


async def _set_status(gateway_id: uuid.UUID, status: str, health: dict[str, Any] | None = None) -> None:
    async with get_sessionmaker()() as db:
        gw = await db.get(Gateway, gateway_id)
        if gw is None or gw.status == "revoked":
            return
        gw.status = status
        if status == "online":
            gw.last_heartbeat_at = datetime.now(UTC)
        if health is not None:
            safe = {k: v for k, v in health.items() if k in ("uptime", "cpu", "streams", "version", "networks", "errors")}
            gw.gateway_metadata = {**(gw.gateway_metadata or {}), "health": safe}
        await db.commit()


async def disconnect(gateway_id: uuid.UUID) -> None:
    link = _links.pop(gateway_id, None)
    if link is not None:
        await link.ws.close(code=4001)
