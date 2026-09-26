from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import auth, cameras, conversations, system, video_sources, vision, voice
from app.core.cache import close_cache
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.video.gateway import VideoGateway

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.assert_production_safe()
    gateway = VideoGateway(settings)
    gateway.purge_workdir()
    from app.services.vision import recover_after_restart

    recovered = await recover_after_restart(gateway)
    if any(recovered.values()):
        logger.info("Recovered jobs after restart: %s", recovered)
    logger.info("Video analyzer: %s", settings.resolved_analyzer_provider)
    if settings.resolved_analyzer_provider == "mock":
        logger.warning("GEMINI_API_KEY not set: using the MOCK video analyzer. Answers are placeholders.")
    warmup = None
    if settings.stt_provider == "mms":
        # Load the local speech model in the background so the first spoken question is not slow (~20 s load).
        from app.voice.factory import get_stt

        stt = get_stt()
        warmup = asyncio.create_task(asyncio.to_thread(stt._load))  # type: ignore[attr-defined]
    from app.cameras import live, media_server
    from app.cameras import service as cameras

    if settings.media_server_managed:
        try:
            media_server.start(settings)
            await media_server.wait_api()
        except Exception:  # noqa: BLE001 - uploaded/linked videos keep working without live cameras
            logger.exception("Media server did not start: live cameras are unavailable")
    restored = await cameras.restore_all()
    if restored:
        logger.info("Restored %d live cameras", restored)
    cameras.start_monitor()
    yield
    cameras.stop_monitor()
    live.stop_all()
    from app.cameras import device_events

    device_events.stop_all()
    if settings.media_server_managed:
        media_server.stop()
    if warmup is not None and not warmup.done():
        warmup.cancel()
    await close_cache()
    await dispose_engine()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="Visionary: Talk to your cameras",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.environment != "production" else None,
        openapi_url="/api/openapi.json" if settings.environment != "production" else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def upload_size_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Runs before the multipart body is spooled to disk, so oversized uploads are refused up front.
        if request.method == "POST" and request.url.path == "/api/video-sources/upload":
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > get_settings().max_upload_bytes + 1024 * 1024:
                limit = get_settings().video_max_upload_mb
                return JSONResponse(status_code=413, content={"detail": f"Video is too large. The upload limit is {limit} MB.", "code": "upload_too_large"})
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "code": exc.code})

    api = APIRouter(prefix="/api")
    for module in (system, auth, video_sources, vision, conversations, voice, cameras):
        api.include_router(module.router)
    app.include_router(api)
    return app


app = create_app()
