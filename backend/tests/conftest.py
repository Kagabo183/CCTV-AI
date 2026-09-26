from __future__ import annotations

import os
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

# Configure before any app import (settings are cached).
os.environ.update(
    ENVIRONMENT="test",
    SECRET_KEY="test-secret-key-that-is-long-enough-for-hs256",
    REDIS_URL="",
    VIDEO_ANALYZER_PROVIDER="mock",
    GEMINI_API_KEY="",
    STT_PROVIDER="mock",
    TTS_PROVIDER="mock",
    VIDEO_URL_ALLOWED_DOMAINS="",
    VISION_ENABLED="false",  # vision tests enable it explicitly with fakes
    VIDEO_UNDERSTANDING_PROVIDERS="gemini",  # never call a real local VLM from tests
    LOCAL_VLM_MODEL="",
    TRANSLATION_PROVIDER="none",  # never load NLLB in tests (a fake translator is used where needed)
    AGENT_LLM="gemini",
)

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from app.db.session import override_engine


@pytest.fixture
async def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[object]:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(get_settings(), "vision_artifacts_dir", tmp_path / "vision_artifacts")
    import app.models  # noqa: F401 - registers every table on Base.metadata, even when a test file never imports them

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    override_engine(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def client(engine: object) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def register(client: httpx.AsyncClient, email: str = "user@example.com") -> dict[str, str]:
    r = await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def fake_import(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace link planning/downloading with an offline fake. Returns the URLs imported."""
    from app.video import ingest
    from app.video.url_safety import check_url_syntax, is_youtube, strip_credentials

    imported: list[str] = []

    async def plan(url: str, settings: ingest.ImportSettings) -> ingest.ImportPlan:
        check_url_syntax(url, settings.policy, streams=True)  # keep the real safety checks
        kind = "site" if is_youtube(url) else ("stream" if url.startswith("rtsp") else "file")
        return ingest.ImportPlan(kind, url, strip_credentials(url), title="Fake title" if kind == "site" else None, is_live=kind == "stream",
                                 details={"protocol": "rtsp"} if kind == "stream" else {})

    async def run(p: ingest.ImportPlan, dest: Path, settings: ingest.ImportSettings, progress=None, tag: str = "") -> dict:  # type: ignore[no-untyped-def]
        imported.append(p.url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        return {"delivery": "imported", "import_kind": p.kind, "original_url": p.display_url, "mime_type": "video/mp4", "duration_seconds": 30.0, "width": 640, "height": 360, "codec": "h264"}

    monkeypatch.setattr(ingest, "plan", plan)
    monkeypatch.setattr(ingest, "run", run)
    return imported


async def wait_imports() -> None:
    from app.services import imports

    while imports._tasks:
        await asyncio.gather(*list(imports._tasks))
