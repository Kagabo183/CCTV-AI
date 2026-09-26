"""The VideoSource abstraction.

Everything above the video gateway (orchestrator, analyzers, API) talks to
sources only through this interface, so adding RTSP/ONVIF/NVR later means
adding a subclass, not redesigning the flow.

Key idea: an analyzer never reads a source directly. It receives a
`MediaHandle`, a local file or a provider-fetchable URI, produced by
`VideoSource.acquire_media()`. For a URL that means "download the file".
For a live camera it will mean "record the requested time window into a
clip". The analyzer does not care which.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class SourceKind(StrEnum):
    URL = "url"
    LOCAL = "local"
    UPLOAD = "upload"
    RTSP = "rtsp"  # a recorded clip of an RTSP stream (link import)
    ONVIF = "onvif"  # legacy name, not used for new sources
    NVR = "nvr"  # an NVR/DVR (its channels are nvr_channel sources)
    CAMERA_RTSP = "camera_rtsp"  # live cameras (app/cameras)
    CAMERA_ONVIF = "camera_onvif"
    CAMERA_HLS = "camera_hls"
    CAMERA_VENDOR = "camera_vendor"
    NVR_CHANNEL = "nvr_channel"


@dataclass(frozen=True)
class TimeWindow:
    """Seconds from the start for recorded media. For live sources: seconds back from now."""

    start: float | None = None
    end: float | None = None


@dataclass(frozen=True)
class PlaybackInfo:
    """How the frontend should play the source.

    direct  - browser loads `url` itself (public video URL)
    youtube - embed player for `url`
    proxy   - backend streams it (local files)
    webrtc  - live camera: `url` returns a short-lived WHEP address (and HLS fallback)
    """

    type: Literal["direct", "youtube", "proxy", "webrtc"]
    url: str


@dataclass
class MediaHandle:
    mime_type: str
    local_path: Path | None = None
    remote_uri: str | None = None  # a URI the analyzer provider can fetch itself (e.g. YouTube for Gemini)
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _cleanup: Callable[[], Awaitable[None]] | None = None

    async def release(self) -> None:
        """Delete temporary copies. Called as soon as the analyzer is done with the media."""
        if self._cleanup is not None:
            await self._cleanup()
            self._cleanup = None


class VideoSource(ABC):
    kind: SourceKind
    is_live: bool = False

    def __init__(self, source_id: uuid.UUID | None, uri: str, metadata: dict[str, Any] | None = None) -> None:
        self.source_id = source_id
        self.uri = uri
        self.metadata = dict(metadata or {})

    @abstractmethod
    async def validate(self) -> dict[str, Any]:
        """Check the source is reachable and acceptable; return metadata to store.

        Raises ValidationFailed with a user-presentable message.
        """

    @abstractmethod
    async def acquire_media(self, workdir: Path, window: TimeWindow | None = None) -> MediaHandle:
        """Produce media the analyzer can consume (a file, a clip, or a remote URI)."""

    @abstractmethod
    def playback(self) -> PlaybackInfo:
        """Describe how a browser should display this source."""
