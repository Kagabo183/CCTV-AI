"""Who may do what with a camera: owner, or a share with a role and explicit permissions."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras import media_server
from app.core.errors import AppError, NotFoundError
from app.models import AuditLog, CameraShare, Gateway, User, VideoSourceRecord

PERMISSIONS = ("live_view", "playback", "download", "ptz", "audio", "talk", "ai_query", "camera_settings")
ROLE_PERMISSIONS = {
    "owner": set(PERMISSIONS) | {"share", "delete"},
    "admin": set(PERMISSIONS) | {"share"},
    "operator": {"live_view", "playback", "download", "ptz", "audio", "talk", "ai_query"},
    "viewer": {"live_view", "playback", "ai_query"},
}


class Forbidden(AppError):
    status_code = 403

    def __init__(self, message: str = "You do not have permission for this camera") -> None:
        super().__init__(message, code="forbidden")


async def role_for(db: AsyncSession, user: User, source: VideoSourceRecord) -> tuple[str | None, set[str]]:
    if source.owner_id == user.id:
        return "owner", ROLE_PERMISSIONS["owner"]
    target = source.parent_id or source.id
    share = (await db.execute(select(CameraShare).where(CameraShare.user_id == user.id, CameraShare.video_source_id.in_([source.id, target])))).scalars().first()
    if share is None:
        return None, set()
    return share.role, ROLE_PERMISSIONS.get(share.role, set()) | set(share.permissions or [])


async def camera_for(db: AsyncSession, user: User, source_id: uuid.UUID, permission: str = "live_view") -> VideoSourceRecord:
    source = await db.get(VideoSourceRecord, source_id)
    if source is None:
        raise NotFoundError("Camera not found")
    role, perms = await role_for(db, user, source)
    if role is None:
        raise NotFoundError("Camera not found")  # do not reveal that it exists
    if permission not in perms:
        raise Forbidden(f"Your role ({role}) does not include '{permission}' for this camera")
    return source


async def audit(db: AsyncSession, user: User | None, action: str, target_type: str, target_id: Any, **detail: Any) -> None:
    db.add(AuditLog(user_id=user.id if user else None, action=action, target_type=target_type, target_id=str(target_id) if target_id else None, detail=detail))


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def authorize_media(db: AsyncSession, payload: dict[str, Any]) -> bool:
    """Decide a MediaMTX auth request (read / publish / playback on a path)."""
    action, path = payload.get("action", ""), (payload.get("path") or "").strip("/")
    user, password = payload.get("user") or "", payload.get("password") or ""
    if user == media_server.INTERNAL_USER and hmac.compare_digest(password, media_server.internal_password()):
        return True
    token = payload.get("token") or ""
    if not token:
        from urllib.parse import parse_qs

        query = parse_qs(payload.get("query") or "")
        token = (query.get("token") or query.get("jwt") or [""])[0] or (password if user in ("", "token") else "")
    if action in ("read", "playback") and token:
        return media_server.check_stream_token(token, path, action) is not None
    if action == "publish" and path.startswith("gw/"):
        parts = path.split("/")
        try:
            gateway = await db.get(Gateway, uuid.UUID(hex=parts[1]))
        except (ValueError, IndexError):
            return False
        return bool(gateway and gateway.status != "revoked" and gateway.secret_hash and user == gateway.id.hex
                    and hmac.compare_digest(secret_hash(password), gateway.secret_hash))
    return False
