"""End-to-end API flow with the mock analyzer (no network, no credentials)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.video.sources.base import MediaHandle
from app.video.sources.url_source import UrlVideoSource
from tests.conftest import register

VIDEO_URL = "https://videos.example.com/gate.mp4"


@pytest.fixture(autouse=True)
def fake_url_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def validate(self: UrlVideoSource) -> dict[str, Any]:
        return {"host": "videos.example.com", "delivery": "download", "mime_type": "video/mp4", "size_bytes": 1234}

    async def acquire(self: UrlVideoSource, workdir: Path, window: Any = None) -> MediaHandle:
        return MediaHandle(mime_type="video/mp4", remote_uri=self.uri)

    monkeypatch.setattr(UrlVideoSource, "validate", validate)
    monkeypatch.setattr(UrlVideoSource, "acquire_media", acquire)


async def _source(client: httpx.AsyncClient, headers: dict[str, str], **body: str) -> dict[str, Any]:
    r = await client.post("/api/video-sources", json={"name": "Irembo", "location": "irembo", "uri": VIDEO_URL, **body}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/video-sources")).status_code == 401


async def test_full_conversation_flow(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    config = (await client.get("/api/config")).json()
    assert config["analyzer_is_mock"] is True and "gemini_api_key" not in str(config).lower()

    source = await _source(client, headers)
    assert source["status"] == "ready" and source["playback"] == {"type": "direct", "url": VIDEO_URL}

    session = (await client.post(f"/api/video-sources/{source['id']}/open", headers=headers)).json()
    assert session["analyzer"] == "mock"

    conv = (await client.post("/api/conversations", json={"video_source_id": source["id"]}, headers=headers)).json()
    assert conv["language"] == "rw"

    first = await client.post(f"/api/conversations/{conv['id']}/messages", json={"question": "Hari abantu bangahe?"}, headers=headers)
    assert first.status_code == 200, first.text
    answer = first.json()["assistant_message"]
    assert "IGERAGEZA" in answer["content"] and "1" in answer["content"]
    assert answer["timestamps"][0]["start_seconds"] == 3.0
    assert answer["metadata"]["analyzer"] == "mock"

    second = await client.post(f"/api/conversations/{conv['id']}/messages", json={"question": "Umwe muri bo yinjiye?"}, headers=headers)
    assert "cya 2" in second.json()["assistant_message"]["content"]  # history reached the analyzer

    detail = (await client.get(f"/api/conversations/{conv['id']}", headers=headers)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    assert detail["title"] == "Hari abantu bangahe?"

    events = (await client.get(f"/api/video-sources/{source['id']}/events", headers=headers)).json()
    assert len(events) == 2 and events[0]["detector"] == "mock"


async def test_voice_turn_with_mock_stt(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    source = await _source(client, headers)
    conv = (await client.post("/api/conversations", json={"video_source_id": source["id"]}, headers=headers)).json()
    r = await client.post(
        f"/api/conversations/{conv['id']}/voice",
        files={"audio": ("q.webm", b"\x1a\x45\xdf\xa3fake", "audio/webm")},
        data={"speak": "true"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"]["is_placeholder"] is True
    assert body["user_message"]["input_mode"] == "voice"
    assert body["audio"] is None and body["tts_available"] is False


async def test_rejects_non_audio_upload(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    r = await client.post("/api/voice/transcribe", files={"audio": ("x.exe", b"MZ", "application/octet-stream")}, headers=headers)
    assert r.status_code == 422


async def test_switches_camera_when_user_names_another(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    parking = await _source(client, headers, name="Parking", location="parikingi")
    gate = await _source(client, headers, name="Gate cam", location="irembo")
    conv = (await client.post("/api/conversations", json={"video_source_id": parking["id"]}, headers=headers)).json()
    r = await client.post(f"/api/conversations/{conv['id']}/messages", json={"question": "Ni iki kiri kuba kuri camera yo ku irembo?"}, headers=headers)
    assert r.json()["switched_to_source"]["id"] == gate["id"]


async def test_users_cannot_see_each_others_data(client: httpx.AsyncClient) -> None:
    alice = await register(client, "alice@example.com")
    bob = await register(client, "bob@example.com")
    source = await _source(client, alice)
    assert (await client.get(f"/api/video-sources/{source['id']}", headers=bob)).status_code == 404
    assert (await client.post("/api/conversations", json={"video_source_id": source["id"]}, headers=bob)).status_code == 404


async def test_unsafe_url_is_rejected_by_api(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # use the real validator
    headers = await register(client)
    r = await client.post("/api/video-sources", json={"name": "x", "uri": "https://169.254.169.254/latest/meta-data"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "unsafe_url"
    r = await client.post("/api/video-sources", json={"name": "x", "kind": "rtsp", "uri": "rtsp://cam/stream"}, headers=headers)
    assert r.status_code == 501


async def test_login_and_cookie_session(client: httpx.AsyncClient) -> None:
    await register(client, "carol@example.com")
    client.cookies.clear()
    bad = await client.post("/api/auth/login", json={"email": "carol@example.com", "password": "wrong-password"})
    assert bad.status_code == 401
    ok = await client.post("/api/auth/login", json={"email": "CAROL@example.com", "password": "correct-horse-battery"})
    assert ok.status_code == 200 and "visionary_session" in ok.cookies
    assert (await client.get("/api/auth/me")).json()["email"] == "carol@example.com"
