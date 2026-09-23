from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import auth, conversations, system, video_sources, vision, voice
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
    VideoGateway(settings).purge_workdir()
    logger.info("Video analyzer: %s", settings.resolved_analyzer_provider)
    if settings.resolved_analyzer_provider == "mock":
        logger.warning("GEMINI_API_KEY not set: using the MOCK video analyzer. Answers are placeholders.")
    yield
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
    for module in (system, auth, video_sources, vision, conversations, voice):
        api.include_router(module.router)
    app.include_router(api)
    return app


app = create_app()
