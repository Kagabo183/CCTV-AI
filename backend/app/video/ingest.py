"""Import any video link into platform storage as a browser-playable MP4.

    plan(url)  ->  how to get it          (fast: one small request, or yt-dlp metadata)
      file     direct video file          downloaded through the SSRF-safe client
      site     page with a video          yt-dlp (YouTube, Vimeo, Facebook, news sites, ...)
      stream   live or segmented stream   ffmpeg records a clip (RTSP cameras, HLS .m3u8, MJPEG)
    run(plan) ->  normalised H.264 MP4 + metadata

Once imported, every source behaves like an upload: it plays from our server
(so the model-box overlay works), local detectors read the file, and Gemini
gets the same file. The original link is kept (without credentials) for reference.

Security: the link's host is validated (public, or in CAMERA_PRIVATE_NETWORKS)
before yt-dlp or ffmpeg touch it. Those tools do their own networking, so a
malicious page could still redirect them; imports therefore run with size/time
limits and only http(s)/rtsp protocols. Downloading third-party content must
respect the source's terms and the video owner's rights.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from app.core.errors import ValidationFailed
from app.video import ffmpeg
from app.video.url_safety import STREAM_SCHEMES, UrlPolicy, check_url_syntax, is_youtube, resolve_public, safe_stream, strip_credentials

logger = logging.getLogger(__name__)

ImportKind = Literal["file", "site", "stream"]
Progress = Callable[[float], None]

_VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".3gp", ".ts", ".flv", ".wmv", ".dav", ".h264", ".264"}
_HLS_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl")


@dataclass
class ImportPlan:
    kind: ImportKind
    url: str  # may contain camera credentials: never persist
    display_url: str
    title: str | None = None
    is_live: bool = False
    duration: float | None = None
    extractor: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportSettings:
    policy: UrlPolicy
    max_bytes: int
    max_seconds: float  # longer recordings are cut
    max_height: int = 1080
    clip_seconds: float = 60.0  # how much of a live stream to record


def _sniff(head: bytes) -> str | None:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "avi"
    if head[:1] == b"\x47" and len(head) > 188 and head[188:189] == b"\x47":
        return "ts"
    if head.startswith(b"FLV"):
        return "flv"
    if head.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "mpeg"
    return None


async def plan(url: str, settings: ImportSettings) -> ImportPlan:
    """Work out how to import a link. Raises ValidationFailed with a user-facing reason."""
    url = url.strip()
    scheme = urlsplit(url).scheme.lower()
    host = check_url_syntax(url, settings.policy, streams=True)
    port = urlsplit(url).port or {"https": 443, "http": 80, "rtsp": 554, "rtsps": 322}.get(scheme, 443)
    await resolve_public(host, port, settings.policy)  # DNS must point at an allowed address
    display = strip_credentials(url)

    if scheme in STREAM_SCHEMES:
        return ImportPlan("stream", url, display, is_live=True, details={"protocol": scheme})
    if is_youtube(url):
        return await _plan_site(url, display, settings)

    head, content_type = b"", ""
    try:
        async with safe_stream(url, settings.policy, headers={"Range": "bytes=0-65535"}, streams=True) as response:
            if response.status_code not in (200, 206):
                raise ValidationFailed(f"The link returned HTTP {response.status_code}", code="url_unreachable")
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type.startswith("multipart/x-mixed-replace"):
                return ImportPlan("stream", url, display, is_live=True, details={"protocol": "mjpeg"})
            async for chunk in response.aiter_bytes():
                head += chunk
                if len(head) >= 65536:
                    break
    except ValidationFailed as exc:
        if exc.code == "unsafe_url":
            raise
        # Some sites refuse plain HTTP clients but work with yt-dlp.
        logger.info("Direct probe failed (%s); trying site extraction", exc.code)
        return await _plan_site(url, display, settings)

    if content_type in _HLS_TYPES or head.lstrip().startswith(b"#EXTM3U"):
        # yt-dlp resolves master playlists and knows live from recorded (VOD):
        # recorded HLS is downloaded whole, live HLS is recorded as a clip.
        return await _plan_site(url, display, settings)
    ext = Path(urlsplit(url).path).suffix.lower()
    if _sniff(head) or content_type.startswith("video/") or (ext in _VIDEO_EXTS and not content_type.startswith("text/")):
        return ImportPlan("file", url, display, details={"content_type": content_type})
    return await _plan_site(url, display, settings)


def cookie_options() -> dict[str, Any]:
    from app.core.config import get_settings

    path = get_settings().ytdlp_cookies_file
    # an empty YTDLP_COOKIES_FILE= parses as Path("."): only a real file counts
    return {"cookiefile": str(path)} if path and str(path) not in ("", ".") and path.is_file() else {}


def friendly_ytdlp_error(message: str) -> str:
    if "not a bot" in message or "Sign in to confirm" in message:
        return ("YouTube is temporarily blocking automated access from this computer (too many requests). "
                "It usually clears within an hour. To avoid it, export YouTube cookies from a signed-in browser "
                "to a cookies.txt file and set YTDLP_COOKIES_FILE.")
    return message


def _ydl_options(settings: ImportSettings, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    h = settings.max_height
    return {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "ignoreconfig": True,  # never read user/system yt-dlp config files
        "noplaylist": True,
        "proxy": "",
        "socket_timeout": 20,
        "js_runtimes": {"node": {}, "deno": {}},
        "ffmpeg_location": ffmpeg.ffmpeg_exe(),
        # Prefer H.264 (decodable everywhere), then anything else; normalised afterwards.
        "format": f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/b[vcodec^=avc1][height<={h}]/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
        "merge_output_format": "mp4",
        "max_filesize": settings.max_bytes,
        **cookie_options(),
        **(extra or {}),
    }


async def _plan_site(url: str, display: str, settings: ImportSettings) -> ImportPlan:
    import yt_dlp

    def extract() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(_ydl_options(settings)) as ydl:
            return ydl.extract_info(url, download=False) or {}

    try:
        info = await asyncio.wait_for(asyncio.to_thread(extract), timeout=60)
    except TimeoutError as exc:
        raise ValidationFailed("The page took too long to respond", code="import_failed") from exc
    except Exception as exc:  # yt-dlp raises many error types
        message = str(exc).replace("ERROR: ", "").split("\n")[0][:200]
        if "not a bot" in message:
            raise ValidationFailed(friendly_ytdlp_error(message), code="youtube_bot_check") from exc
        raise ValidationFailed(f"No playable video found at this link ({message})", code="no_video_found") from exc
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise ValidationFailed("This link is a playlist with no playable videos", code="no_video_found")
        info = entries[0]
    return ImportPlan(
        "site",
        info.get("webpage_url") or url,
        display,
        title=info.get("title"),
        is_live=bool(info.get("is_live")),
        duration=info.get("duration"),
        extractor=info.get("extractor_key"),
        details={"stream_url": info.get("url") if info.get("is_live") else None},
    )


async def run(p: ImportPlan, dest: Path, settings: ImportSettings, progress: Progress | None = None, tag: str = "") -> dict[str, Any]:
    """Execute a plan: produce `dest` (MP4) and return metadata to store.

    `tag` goes into the work folder name so a cancelled import's helper
    processes (ffmpeg started by yt-dlp) can be found and stopped."""
    report = progress or (lambda _: None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"import_{tag}_" if tag else "import_", dir=dest.parent))
    try:
        if p.kind == "file":
            raw = await _download_file(p, work, settings, report)
        elif p.kind == "site" and not p.is_live:
            raw = await asyncio.to_thread(_download_site, p, work, settings, report)
        else:
            stream_url = p.details.get("stream_url") or p.url
            seconds = min(settings.clip_seconds, settings.max_seconds)
            report(0.05)
            mjpeg = p.details.get("protocol") == "mjpeg"
            info = await asyncio.to_thread(
                ffmpeg.capture, stream_url, work / "capture.mp4", seconds=seconds, max_height=settings.max_height,
                wallclock=mjpeg, fps=15 if mjpeg else None,
            )
            raw = work / "capture.mp4"
            if info.duration is None or info.duration < 0.5:
                raise ValidationFailed("The stream produced no video", code="import_failed")
        report(0.9)
        info = await asyncio.to_thread(ffmpeg.normalize, raw, dest, max_height=settings.max_height, max_seconds=settings.max_seconds)
        report(1.0)
    except ffmpeg.FfmpegError as exc:
        raise ValidationFailed(f"Could not process this video: {exc}", code="import_failed") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    meta: dict[str, Any] = {
        "delivery": "imported",
        "import_kind": p.kind,
        "original_url": p.display_url,
        "mime_type": "video/mp4",
        "size_bytes": dest.stat().st_size,
        **info.as_metadata(),
    }
    if p.title:
        meta["title"] = p.title[:300]
    if p.extractor:
        meta["extractor"] = p.extractor
    if p.is_live:
        meta["live_clip_seconds"] = round(info.duration or 0, 1)
    if p.duration and info.duration and p.duration > info.duration + 5:
        meta["truncated_from_seconds"] = p.duration
    return meta


async def _download_file(p: ImportPlan, work: Path, settings: ImportSettings, report: Progress) -> Path:
    path = work / f"{uuid.uuid4().hex}{Path(urlsplit(p.url).path).suffix.lower() or '.video'}"
    written = 0
    async with safe_stream(p.url, settings.policy, streams=True) as response:
        if response.status_code != 200:
            raise ValidationFailed(f"The link returned HTTP {response.status_code}", code="url_unreachable")
        total = int(response.headers.get("content-length") or 0)
        if total > settings.max_bytes:
            raise ValidationFailed(f"Video is too large ({total // 2**20} MB). Limit is {settings.max_bytes // 2**20} MB.")
        with path.open("wb") as fh:
            async for chunk in response.aiter_bytes(1024 * 512):
                written += len(chunk)
                if written > settings.max_bytes:
                    raise ValidationFailed(f"Video exceeds the {settings.max_bytes // 2**20} MB limit")
                fh.write(chunk)
                if total:
                    report(0.85 * written / total)
    return path


def _download_site(p: ImportPlan, work: Path, settings: ImportSettings, report: Progress) -> Path:
    import yt_dlp

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                report(0.85 * min(d.get("downloaded_bytes", 0) / total, 1.0))

    extra: dict[str, Any] = {"outtmpl": str(work / "media.%(ext)s"), "progress_hooks": [hook]}
    if p.duration and p.duration > settings.max_seconds:
        # Only fetch the first max_seconds of long videos.
        from yt_dlp.utils import download_range_func

        extra["download_ranges"] = download_range_func(None, [(0, settings.max_seconds)])
        extra["force_keyframes_at_cuts"] = False
    with yt_dlp.YoutubeDL(_ydl_options(settings, extra)) as ydl:
        try:
            ydl.download([p.url])
        except Exception as exc:
            message = str(exc).replace("ERROR: ", "").split("\n")[0][:200]
            raise ValidationFailed(f"Download failed: {message}", code="import_failed") from exc
    files = [f for f in work.iterdir() if f.is_file() and f.suffix not in (".part", ".ytdl")]
    if not files:
        raise ValidationFailed("Download produced no video file", code="import_failed")
    return max(files, key=lambda f: f.stat().st_size)
