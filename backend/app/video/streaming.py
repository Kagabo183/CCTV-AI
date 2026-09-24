"""Analyse online videos in place, without downloading them.

YouTube links play in the embedded YouTube player straight away. For local
vision (and frame-based video AI) we ask yt-dlp for the direct media URL of a
video-only rendition and let OpenCV/FFmpeg read frames from it over HTTP.
Nothing is written to disk.

Security: the page URL was validated when the source was registered. The
resolved media URL is only accepted on YouTube's own media hosts, so a crafted
page cannot point OpenCV at an internal address.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from app.core.errors import ValidationFailed

_MEDIA_HOSTS = (".googlevideo.com", ".youtube.com", ".ytimg.com")
_TTL_SECONDS = 2 * 3600  # signed media URLs last ~6 h; refresh well before
_cache: dict[tuple[str, int], tuple[float, StreamInfo]] = {}
_lock = Lock()


@dataclass(frozen=True)
class StreamInfo:
    url: str  # signed, short-lived: never persist or log
    width: int | None
    height: int | None
    fps: float | None
    duration: float | None
    vcodec: str | None

    def as_metadata(self) -> dict[str, Any]:
        return {k: v for k, v in {"width": self.width, "height": self.height, "fps": self.fps, "duration_seconds": self.duration, "codec": self.vcodec}.items() if v}


def is_streamed(metadata: dict[str, Any]) -> bool:
    """A source analysed from its online stream (no stored copy)."""
    return metadata.get("delivery") == "youtube" and not metadata.get("stored_path")


def _disk_cache() -> Any:
    from app.core.config import get_settings

    return get_settings().video_work_dir / "stream_urls.json"


def _load_disk(key: str) -> StreamInfo | None:
    # Kept on disk (in the work dir, not the database) so restarts do not ask YouTube again:
    # repeated lookups are what trigger its "confirm you're not a bot" block.
    try:
        entry = json.loads(_disk_cache().read_text()).get(key)
    except (OSError, ValueError):
        return None
    if not entry or time.time() - entry.get("at", 0) > _TTL_SECONDS:
        return None
    return StreamInfo(**entry["info"])


def _save_disk(key: str, info: StreamInfo) -> None:
    path = _disk_cache()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    now = time.time()
    data = {k: v for k, v in data.items() if now - v.get("at", 0) < _TTL_SECONDS}
    data[key] = {"at": now, "info": asdict(info)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass


def resolve(page_url: str, max_height: int = 720) -> StreamInfo:
    """Direct media URL for a video page (blocking; call in a thread)."""
    key = (page_url, max_height)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _TTL_SECONDS:
            return hit[1]
        disk = _load_disk(f"{max_height}|{page_url}")
        if disk is not None:
            _cache[key] = (time.monotonic(), disk)
            return disk

    import yt_dlp

    from app.video import ffmpeg, ingest

    h = max_height
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "proxy": "",
        "socket_timeout": 20,
        "js_runtimes": {"node": {}, "deno": {}},
        "ffmpeg_location": ffmpeg.ffmpeg_exe(),
        **ingest.cookie_options(),
        # Video only (the detector needs no audio), plain HTTP (not HLS/DASH manifests), H.264 preferred.
        "format": f"bv*[vcodec^=avc1][height<={h}][protocol^=http]/bv*[height<={h}][protocol^=http]/b[height<={h}][protocol^=http]",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False) or {}
    except Exception as exc:  # yt-dlp raises many error types
        message = str(exc).replace("ERROR: ", "").split("\n")[0][:200]
        raise ValidationFailed(ingest.friendly_ytdlp_error(f"Could not open the online video for analysis ({message})"), code="stream_unavailable") from exc
    url = info.get("url")
    host = (urlsplit(url or "").hostname or "").lower()
    if not url or not host.endswith(_MEDIA_HOSTS):
        raise ValidationFailed("The online video did not offer a stream we can analyse", code="stream_unavailable")
    result = StreamInfo(
        url=url,
        width=info.get("width"),
        height=info.get("height"),
        fps=info.get("fps"),
        duration=info.get("duration"),
        vcodec=(info.get("vcodec") or "").split(".")[0] or None,
    )
    with _lock:
        _cache[key] = (time.monotonic(), result)
        _save_disk(f"{max_height}|{page_url}", result)
    return result
