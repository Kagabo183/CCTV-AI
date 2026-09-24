"""End-to-end API flow with the mock analyzer (no network, no credentials)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.conftest import register, wait_imports

VIDEO_URL = "https://videos.example.com/gate.mp4"


@pytest.fixture(autouse=True)
def _offline_imports(fake_import: list[str]) -> None:
    """Every test here registers links; imports are faked (no network)."""


async def _source(client: httpx.AsyncClient, headers: dict[str, str], **body: str) -> dict[str, Any]:
    r = await client.post("/api/video-sources", json={"name": "Irembo", "location": "irembo", "uri": VIDEO_URL, **body}, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "importing"
    await wait_imports()
    source = (await client.get(f"/api/video-sources/{r.json()['id']}", headers=headers)).json()
    assert source["status"] == "ready", source
    return source


async def test_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/video-sources")).status_code == 401


async def test_full_conversation_flow(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    config = (await client.get("/api/config")).json()
    assert config["analyzer_is_mock"] is True and "gemini_api_key" not in str(config).lower()

    source = await _source(client, headers)
    assert source["uri"] == VIDEO_URL and source["metadata"]["stored_path"].endswith(".mp4")
    assert source["playback"] == {"type": "proxy", "url": f"/api/video-sources/{source['id']}/stream"}  # served by us: box overlay works

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
    r = await client.post("/api/video-sources", json={"name": "x", "uri": "rtsp://admin:pw@192.168.1.64:554/stream"}, headers=headers)
    assert r.status_code == 422 and "CAMERA_PRIVATE_NETWORKS" in r.json()["detail"]  # LAN cameras need an explicit allowlist
    r = await client.post("/api/video-sources", json={"name": "x", "kind": "onvif", "uri": "onvif://cam"}, headers=headers)
    assert r.status_code == 501


async def test_login_and_cookie_session(client: httpx.AsyncClient) -> None:
    await register(client, "carol@example.com")
    client.cookies.clear()
    bad = await client.post("/api/auth/login", json={"email": "carol@example.com", "password": "wrong-password"})
    assert bad.status_code == 401
    ok = await client.post("/api/auth/login", json={"email": "CAROL@example.com", "password": "correct-horse-battery"})
    assert ok.status_code == 200 and "visionary_session" in ok.cookies
    assert (await client.get("/api/auth/me")).json()["email"] == "carol@example.com"


async def test_links_of_every_kind_are_imported(client: httpx.AsyncClient, fake_import: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "camera_private_networks", ["192.168.1.0/24"])

    headers = await register(client)
    yt = (await client.post("/api/video-sources", json={"uri": "https://www.youtube.com/watch?v=1gi5qn1khVk"}, headers=headers)).json()
    # YouTube is watched in place: ready at once, plays in the YouTube player, never downloaded
    assert yt["status"] == "ready" and yt["playback"]["type"] == "youtube" and yt["metadata"]["delivery"] == "youtube"
    cam = (await client.post("/api/video-sources", json={"name": "Gate", "uri": "rtsp://admin:secret@192.168.1.64:554/Streaming/Channels/101", "clip_seconds": 20}, headers=headers)).json()
    await wait_imports()

    yt = (await client.get(f"/api/video-sources/{yt['id']}", headers=headers)).json()
    assert yt["name"] == "Fake title" and yt["status"] == "ready" and "stored_path" not in yt["metadata"]
    assert "https://www.youtube.com/watch?v=1gi5qn1khVk" not in fake_import

    cam = (await client.get(f"/api/video-sources/{cam['id']}", headers=headers)).json()
    assert cam["kind"] == "rtsp" and cam["status"] == "ready"
    assert "secret" not in str(cam) and cam["uri"] == "rtsp://192.168.1.64:554/Streaming/Channels/101"  # password never stored
    assert "rtsp://admin:secret@192.168.1.64:554/Streaming/Channels/101" in fake_import  # but it was used for the capture

    # stored copies are deleted with the source
    stored = get_settings().upload_dir / cam["metadata"]["stored_path"]
    assert stored.exists()
    assert (await client.delete(f"/api/video-sources/{cam['id']}", headers=headers)).status_code == 204
    assert not stored.exists()


async def test_failed_import_is_reported_on_the_source(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.errors import ValidationFailed
    from app.video import ingest

    async def broken_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValidationFailed("The stream produced no video", code="import_failed")

    monkeypatch.setattr(ingest, "run", broken_run)
    headers = await register(client)
    r = await client.post("/api/video-sources", json={"uri": VIDEO_URL}, headers=headers)
    await wait_imports()
    source = (await client.get(f"/api/video-sources/{r.json()['id']}", headers=headers)).json()
    assert source["status"] == "error" and source["status_message"] == "The stream produced no video"
