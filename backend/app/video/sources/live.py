"""Live camera sources (RTSP, ONVIF, HLS, NVR channel, vendor, via gateway).

The camera itself is reached by the media server (app/cameras/media_server.py). Here:

* validate()      -> nothing to do: cameras are probed by the camera service when added
* acquire_media() -> a clip for the video AI: the last `window` seconds recorded from the live stream,
                     or, for a past window, the recording (MediaMTX playback) when the camera records
* playback()      -> type "webrtc": the browser asks /api/cameras/{id}/live for a short-lived WHEP URL
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.video.sources.base import MediaHandle, PlaybackInfo, SourceKind, TimeWindow, VideoSource

LIVE_KINDS = {"camera_rtsp", "camera_onvif", "camera_hls", "camera_vendor", "nvr_channel"}
CAMERA_KINDS = LIVE_KINDS | {"nvr"}


class CameraLiveSource(VideoSource):
    is_live = True

    def __init__(self, kind: SourceKind, source_id: uuid.UUID | None, uri: str, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(source_id, uri, metadata)
        self.kind = kind

    async def validate(self) -> dict[str, Any]:
        return {}

    def media_path(self, role: str = "main") -> str:
        from app.cameras import media_server

        gateway = self.metadata.get("gateway_id")
        if gateway:
            return media_server.gateway_path(uuid.UUID(hex=gateway), self.source_id, role)
        return media_server.camera_path(self.source_id, role)

    async def acquire_media(self, workdir: Path, window: TimeWindow | None = None) -> MediaHandle:
        from app.cameras import media_server
        from app.video import ffmpeg

        workdir.mkdir(parents=True, exist_ok=True)
        dest = workdir / f"{uuid.uuid4().hex}.mp4"
        seconds = 20.0
        if window and window.start is not None and window.end is not None:
            seconds = max(3.0, min(120.0, window.start - window.end))
        back = window.start if window and window.start is not None else seconds
        if back > seconds + 5 and self.metadata.get("recording"):
            # a past moment: cut it from the recording
            import httpx

            start = datetime.now(UTC) - timedelta(seconds=back)
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                r = await client.get(media_server.playback_url(self.media_path("main"), start, seconds),
                                     auth=(media_server.INTERNAL_USER, media_server.internal_password()))
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
        if not dest.exists():
            await asyncio.to_thread(ffmpeg.capture, media_server.internal_rtsp_url(self.media_path("main")), dest, seconds=seconds, max_height=720)

        async def cleanup() -> None:
            dest.unlink(missing_ok=True)

        return MediaHandle(mime_type="video/mp4", local_path=dest, size_bytes=dest.stat().st_size, metadata={"live_clip_seconds": seconds}, _cleanup=cleanup)

    def playback(self) -> PlaybackInfo:
        return PlaybackInfo(type="webrtc", url=f"/api/cameras/{self.source_id}/live")
