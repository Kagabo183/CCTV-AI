"""Video URL source (Phase 1's primary source)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.core.errors import ValidationFailed
from app.video.sources.base import MediaHandle, PlaybackInfo, SourceKind, TimeWindow, VideoSource
from app.video.url_safety import UrlPolicy, check_url_syntax, is_youtube, safe_stream

_EXT_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".3gp": "video/3gpp",
}


def _sniff_mime(head: bytes) -> str | None:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    if head.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video/mpeg"
    return None


def youtube_video_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        return parts.path.lstrip("/").split("/")[0] or None
    if parts.path == "/watch":
        return (parse_qs(parts.query).get("v") or [None])[0]
    for prefix in ("/shorts/", "/embed/", "/live/"):
        if parts.path.startswith(prefix):
            return parts.path[len(prefix):].split("/")[0] or None
    return None


class UrlVideoSource(VideoSource):
    kind = SourceKind.URL

    def __init__(self, source_id: uuid.UUID | None, uri: str, metadata: dict[str, Any] | None, *, policy: UrlPolicy, max_bytes: int) -> None:
        super().__init__(source_id, uri.strip(), metadata)
        self.policy = policy
        self.max_bytes = max_bytes

    @property
    def youtube(self) -> bool:
        return is_youtube(self.uri)

    async def validate(self) -> dict[str, Any]:
        host = check_url_syntax(self.uri, self.policy)
        if self.youtube:
            video_id = youtube_video_id(self.uri)
            if not video_id:
                raise ValidationFailed("This YouTube link does not point to a video")
            # Fetched by the analyzer provider, never by our server.
            return {"host": host, "delivery": "youtube", "youtube_id": video_id, "mime_type": "video/*"}

        async with safe_stream(self.uri, self.policy, headers={"Range": "bytes=0-4095"}) as response:
            if response.status_code not in (200, 206):
                raise ValidationFailed(f"The video URL returned HTTP {response.status_code}", code="url_unreachable")
            head = b""
            async for chunk in response.aiter_bytes():
                head += chunk
                if len(head) >= 4096:
                    break
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            size = _total_size(response.headers)
            final_url = str(response.url)

        ext_mime = _EXT_MIME.get(Path(urlsplit(self.uri).path).suffix.lower())
        sniffed = _sniff_mime(head)
        if content_type.startswith(("text/", "application/json")) or content_type.endswith(("html", "xml")):
            raise ValidationFailed("The URL points to a web page, not a video file. Use a direct link to the video.")
        mime = sniffed or (content_type if content_type.startswith("video/") else None) or ext_mime
        if mime is None:
            raise ValidationFailed("Could not recognise a video at this URL (supported: MP4, MOV, WebM, MKV, AVI, MPEG)")
        if size is not None and size > self.max_bytes:
            raise ValidationFailed(f"Video is too large ({size // (1024 * 1024)} MB). Limit is {self.max_bytes // (1024 * 1024)} MB.")
        return {"host": host, "delivery": "download", "mime_type": mime, "size_bytes": size, "redirected": final_url != self.uri}

    async def acquire_media(self, workdir: Path, window: TimeWindow | None = None) -> MediaHandle:
        # Recorded video: the whole file is the clip. `window` matters for live sources.
        if self.youtube:
            return MediaHandle(mime_type="video/*", remote_uri=self.uri, metadata={"delivery": "youtube"})

        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / f"{uuid.uuid4().hex}.video"
        written = 0
        try:
            async with safe_stream(self.uri, self.policy) as response:
                if response.status_code != 200:
                    raise ValidationFailed(f"The video URL returned HTTP {response.status_code}", code="url_unreachable")
                with path.open("wb") as fh:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise ValidationFailed("Video exceeds the maximum download size")
                        fh.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        async def cleanup() -> None:
            path.unlink(missing_ok=True)

        with path.open("rb") as fh:
            head = fh.read(16)
        mime = self.metadata.get("mime_type") or _sniff_mime(head) or "video/mp4"
        return MediaHandle(mime_type=mime, local_path=path, size_bytes=written, _cleanup=cleanup)

    def playback(self) -> PlaybackInfo:
        if self.youtube:
            return PlaybackInfo(type="youtube", url=f"https://www.youtube-nocookie.com/embed/{youtube_video_id(self.uri)}")
        return PlaybackInfo(type="direct", url=self.uri)


def _total_size(headers: Any) -> int | None:
    content_range = headers.get("content-range")  # bytes 0-4095/123456
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[1]
        if total.isdigit():
            return int(total)
    length = headers.get("content-length")
    if length and length.isdigit() and "content-range" not in headers:
        return int(length)
    return None

