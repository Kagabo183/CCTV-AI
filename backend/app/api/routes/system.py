from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.analyzers.factory import get_video_analyzer
from app.api.deps import DbSession, Gateway
from app.core.cache import get_cache
from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.api import PublicConfigOut
from app.voice.factory import get_stt, get_tts

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(db: DbSession) -> dict[str, object]:
    try:
        await db.execute(text("SELECT 1"))
        database = True
    except Exception:
        database = False
    return {"status": "ok" if database else "degraded", "database": database, "redis": await get_cache().ping()}


@router.get("/config", response_model=PublicConfigOut)
async def public_config(gw: Gateway) -> PublicConfigOut:
    """Non-secret capabilities so the UI can label mock/placeholder providers."""
    settings = get_settings()
    try:
        analyzer = get_video_analyzer().name
    except AppError:
        analyzer = "unconfigured"
    try:
        stt = get_stt()
        stt_name, stt_placeholder = stt.name, stt.is_placeholder
    except AppError:
        stt_name, stt_placeholder = "unconfigured", True
    try:
        tts = get_tts()
        tts_name, tts_placeholder = tts.name, tts.is_placeholder
    except AppError:
        tts_name, tts_placeholder = "unconfigured", True
    return PublicConfigOut(
        analyzer=analyzer,
        analyzer_is_mock=analyzer in ("mock", "unconfigured"),
        stt_provider=stt_name,
        stt_is_placeholder=stt_placeholder,
        tts_provider=tts_name,
        tts_is_placeholder=tts_placeholder,
        source_kinds=[k.value for k in gw.enabled_kinds()],
        default_language=settings.default_language,
    )
