"""Live camera sources: interface only, NOT implemented in Phase 1.

These classes pin down how live sources fit the VideoSource contract so the
rest of the system needs no redesign when they arrive:

* validate()      -> connect (RTSP DESCRIBE / ONVIF GetProfiles), read codec,
                     resolution and fps, and store them as metadata.
* acquire_media() -> record `window` (e.g. the last 60 s) from a ring buffer
                     kept by an ingest worker into a clip file and return it.
                     The analyzer receives a normal MediaHandle.
* playback()      -> the backend re-publishes the stream as HLS/WebRTC
                     (type="proxy"). Browsers cannot play RTSP.

Camera credentials belong in a secret store referenced from the future
`cameras` table, not in video_sources.uri.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.errors import NotSupportedYet
from app.video.sources.base import MediaHandle, PlaybackInfo, SourceKind, TimeWindow, VideoSource


class _LiveSourceNotImplemented(VideoSource):
    is_live = True

    async def validate(self) -> dict[str, Any]:
        raise NotSupportedYet(f"{self.kind.value.upper()} sources are planned but not available yet")

    async def acquire_media(self, workdir: Path, window: TimeWindow | None = None) -> MediaHandle:
        raise NotSupportedYet(f"{self.kind.value.upper()} sources are planned but not available yet")

    def playback(self) -> PlaybackInfo:
        return PlaybackInfo(type="proxy", url=f"/api/video-sources/{self.source_id}/live")


class RtspCameraSource(_LiveSourceNotImplemented):
    kind = SourceKind.RTSP


class OnvifCameraSource(_LiveSourceNotImplemented):
    kind = SourceKind.ONVIF


class NvrChannelSource(_LiveSourceNotImplemented):
    kind = SourceKind.NVR
