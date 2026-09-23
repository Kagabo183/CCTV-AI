from __future__ import annotations

import os
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
