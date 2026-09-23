"""Local video files. A development convenience, disabled in production.

Only files under LOCAL_VIDEO_DIR can be referenced (no path traversal), so a
user cannot point the server at arbitrary files on disk.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.core.errors import ValidationFailed
from app.video.sources.base import MediaHandle, PlaybackInfo, SourceKind, TimeWindow, VideoSource
from app.video.sources.url_source import _EXT_MIME


class LocalFileVideoSource(VideoSource):
    kind = SourceKind.LOCAL

    def __init__(self, source_id: uuid.UUID | None, uri: str, metadata: dict[str, Any] | None, *, root: Path) -> None:
        super().__init__(source_id, uri, metadata)
        self.root = root.resolve()

    def _path(self) -> Path:
        candidate = (self.root / self.uri).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValidationFailed("Local videos must be inside the configured video directory")
        return candidate

    async def validate(self) -> dict[str, Any]:
        path = self._path()
        if not path.is_file():
            raise ValidationFailed(f"File not found in the local video directory: {self.uri}")
        mime = _EXT_MIME.get(path.suffix.lower())
        if mime is None:
            raise ValidationFailed("Unsupported video file type")
        return {"delivery": "local", "mime_type": mime, "size_bytes": path.stat().st_size}

    async def acquire_media(self, workdir: Path, window: TimeWindow | None = None) -> MediaHandle:
        path = self._path()
        # The file is not ours: no cleanup.
        return MediaHandle(mime_type=self.metadata.get("mime_type", "video/mp4"), local_path=path, size_bytes=path.stat().st_size)

    def playback(self) -> PlaybackInfo:
        return PlaybackInfo(type="proxy", url=f"/api/video-sources/{self.source_id}/stream")

    def file_path(self) -> Path:
        return self._path()
