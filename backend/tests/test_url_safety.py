from __future__ import annotations

import socket

import pytest

from app.video.sources.url_source import UrlVideoSource, _sniff_mime, youtube_video_id
from app.video.url_safety import UnsafeUrlError, UrlPolicy, check_url_syntax, is_public_ip, resolve_public

POLICY = UrlPolicy()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/video.mp4",  # http disabled by default
        "ftp://example.com/video.mp4",
        "file:///etc/passwd",
        "https://localhost/video.mp4",
        "https://127.0.0.1/video.mp4",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/cam.mp4",
        "https://192.168.1.10/cam.mp4",
        "https://[::1]/cam.mp4",
        "https://[::ffff:127.0.0.1]/cam.mp4",
        "https://user:pass@example.com/video.mp4",
        "https://example.com:6379/video.mp4",
        "https://nvr.internal/video.mp4",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        check_url_syntax(url, POLICY)


def test_accepts_public_https() -> None:
    assert check_url_syntax("https://cdn.example.com/a/b.mp4", POLICY) == "cdn.example.com"


def test_allowlist() -> None:
    policy = UrlPolicy(allowed_domains=("example.com",))
    assert check_url_syntax("https://videos.example.com/a.mp4", policy)
    with pytest.raises(UnsafeUrlError):
        check_url_syntax("https://evil-example.com/a.mp4", policy)


def test_http_opt_in() -> None:
    assert check_url_syntax("http://example.com/a.mp4", UrlPolicy(allow_http=True))


def test_public_ip_classification() -> None:
    assert is_public_ip("8.8.8.8")
    for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "100.64.0.1", "169.254.169.254", "::1", "fe80::1", "224.0.0.1"):
        assert not is_public_ip(ip), ip


async def test_dns_resolving_to_private_address_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protects against public hostnames that point at internal services (DNS rebinding)."""

    async def fake_getaddrinfo(host, port, **kwargs):  # type: ignore[no-untyped-def]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        await resolve_public("rebind.example.com", 443)


def test_sniff_and_youtube_parsing() -> None:
    assert _sniff_mime(b"\x00\x00\x00\x18ftypmp42") == "video/mp4"
    assert _sniff_mime(b"\x1a\x45\xdf\xa3....") == "video/webm"
    assert _sniff_mime(b"<!DOCTYPE html>") is None
    assert youtube_video_id("https://www.youtube.com/watch?v=abc123") == "abc123"
    assert youtube_video_id("https://youtu.be/xyz") == "xyz"
    assert youtube_video_id("https://www.youtube.com/channel/foo") is None


async def test_youtube_validation_needs_no_server_fetch() -> None:
    source = UrlVideoSource(None, "https://www.youtube.com/watch?v=abc123", None, policy=POLICY, max_bytes=10)
    meta = await source.validate()
    assert meta["delivery"] == "youtube"
    assert source.playback().type == "youtube"
