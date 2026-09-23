from __future__ import annotations

import httpx

from app.core.config import get_settings
from tests.conftest import register

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 5000


async def _upload(client: httpx.AsyncClient, headers: dict[str, str], data: bytes = MP4, filename: str = "gate.mp4") -> httpx.Response:
    return await client.post(
        "/api/video-sources/upload",
        files={"file": (filename, data, "video/mp4")},
        data={"name": "Uploaded gate", "location": "irembo"},
        headers=headers,
    )


async def test_upload_register_stream_ask_delete(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    r = await _upload(client, headers)
    assert r.status_code == 201, r.text
    source = r.json()
    assert source["kind"] == "upload" and source["metadata"]["size_bytes"] == len(MP4)
    assert source["playback"] == {"type": "proxy", "url": f"/api/video-sources/{source['id']}/stream"}
    stored = get_settings().upload_dir / source["uri"]
    assert stored.is_file() and stored.read_bytes() == MP4

    stream = await client.get(f"/api/video-sources/{source['id']}/stream", headers={**headers, "Range": "bytes=0-11"})
    assert stream.status_code == 206 and stream.content == MP4[:12]

    conv = (await client.post("/api/conversations", json={"video_source_id": source["id"]}, headers=headers)).json()
    ask = await client.post(f"/api/conversations/{conv['id']}/messages", json={"question": "Hari abantu bangahe?"}, headers=headers)
    assert ask.status_code == 200, ask.text

    assert (await client.delete(f"/api/video-sources/{source['id']}", headers=headers)).status_code == 204
    assert not stored.exists()


async def test_upload_rejects_non_video(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    r = await _upload(client, headers, b"<html><script>alert(1)</script></html>", "evil.mp4")
    assert r.status_code == 422
    assert not any(get_settings().upload_dir.rglob("*.*"))


async def test_upload_size_limit(client: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "video_max_upload_mb", 0)
    headers = await register(client)
    r = await _upload(client, headers, MP4 * 300)  # > 1 MB: refused by the middleware before parsing
    assert r.status_code == 413
    r = await _upload(client, headers)  # small but over a 0 MB limit: refused while streaming
    assert r.status_code == 422
    assert not any(get_settings().upload_dir.rglob("*.*"))


async def test_other_users_cannot_stream_uploads(client: httpx.AsyncClient) -> None:
    alice = await register(client, "alice@example.com")
    bob = await register(client, "bob@example.com")
    source = (await _upload(client, alice)).json()
    assert (await client.get(f"/api/video-sources/{source['id']}/stream", headers=bob)).status_code == 404


async def test_upload_kind_cannot_be_registered_by_path(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    r = await client.post("/api/video-sources", json={"name": "x", "kind": "upload", "uri": "../../etc/passwd"}, headers=headers)
    assert r.status_code == 422
