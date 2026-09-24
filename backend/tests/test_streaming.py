from types import SimpleNamespace

import pytest

from app.api.routes.video_sources import _stream_metadata, _streamable
from app.core.errors import ValidationFailed
from app.video import streaming
from app.video.ingest import ImportPlan


def test_recorded_youtube_is_watched_in_place() -> None:
    plan = ImportPlan("site", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", title="Bunny", duration=635)
    assert _streamable(plan)
    meta = _stream_metadata(plan)
    assert meta["delivery"] == "youtube" and meta["youtube_id"] == "aqz-KE-bpKQ" and meta["duration_seconds"] == 635
    assert streaming.is_streamed(meta)


def test_live_or_other_sites_still_import() -> None:
    assert not _streamable(ImportPlan("site", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "x", is_live=True))
    assert not _streamable(ImportPlan("site", "https://vimeo.com/76979871", "x"))
    assert not _streamable(ImportPlan("file", "https://example.com/a.mp4", "x"))
    assert not streaming.is_streamed({"delivery": "youtube", "stored_path": "u/s.mp4"})


class _FakeYDL:
    def __init__(self, url: str) -> None:
        self.url = url

    def __call__(self, opts):  # type: ignore[no-untyped-def]
        return self

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *a):  # type: ignore[no-untyped-def]
        return False

    def extract_info(self, url, download=False):  # type: ignore[no-untyped-def]
        return {"url": self.url, "width": 1280, "height": 720, "fps": 30, "duration": 60, "vcodec": "avc1.4d401f"}


def test_stream_url_must_be_on_youtube_media_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp

    streaming._cache.clear()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL("http://169.254.169.254/latest/meta-data"))
    with pytest.raises(ValidationFailed):
        streaming.resolve("https://www.youtube.com/watch?v=evil0000000")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL("https://rr1---sn-abc.googlevideo.com/videoplayback?x=1"))
    info = streaming.resolve("https://www.youtube.com/watch?v=good0000000")
    assert info.height == 720 and info.vcodec == "avc1" and info.as_metadata()["duration_seconds"] == 60
    assert SimpleNamespace(**info.__dict__).url.startswith("https://rr1")


def test_empty_cookie_setting_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path

    from app.core.config import get_settings
    from app.video.ingest import cookie_options

    monkeypatch.setattr(get_settings(), "ytdlp_cookies_file", Path(""))
    assert cookie_options() == {}
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(get_settings(), "ytdlp_cookies_file", cookies)
    assert cookie_options() == {"cookiefile": str(cookies)}
