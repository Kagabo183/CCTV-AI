"""Uploaded video files.

Users can upload a recording (e.g. exported from an NVR) when they don't have
a URL. Files are stored under UPLOAD_DIR/<user_id>/<random>.<ext> with
server-generated names, and are deleted when the source is deleted. Like URL
media, they reach the analyzer as a MediaHandle, so nothing downstream changes.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from app.core.errors import ValidationFailed
from app.video.sources.base import SourceKind
from app.video.sources.local_source import LocalFileVideoSource
from app.video.sources.url_source import _EXT_MIME, _sniff_mime

_CHUNK = 1024 * 1024
_MIME_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-msvideo": ".avi", "video/mpeg": ".mpg"}


class UploadedVideoSource(LocalFileVideoSource):
    """Same mechanics as a local file, but the file is owned by the platform."""

    kind = SourceKind.UPLOAD

    def delete_file(self) -> None:
        self._path().unlink(missing_ok=True)


async def store_upload(upload: UploadFile, root: Path, owner_id: uuid.UUID, max_bytes: int) -> tuple[str, dict[str, Any]]:
    """Stream an upload to disk, verifying it is really a video. Returns (relative path, metadata)."""
    original = Path(upload.filename or "video").name[:200]
    ext_mime = _EXT_MIME.get(Path(original).suffix.lower())

    head = await upload.read(4096)
    mime = _sniff_mime(head)
    if mime is None:
        # Matroska/3GP etc. are not covered by the sniffer; accept on extension only if the header isn't text.
        if ext_mime and not head.lstrip().startswith((b"<", b"{", b"#!")):
            mime = ext_mime
        else:
            raise ValidationFailed("This file is not a supported video (MP4, MOV, WebM, MKV, AVI, MPEG)")

    user_dir = root.resolve() / str(owner_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    relative = f"{owner_id}/{uuid.uuid4().hex}{_MIME_EXT.get(mime) or Path(original).suffix.lower() or '.mp4'}"
    path = root.resolve() / relative
    written = 0
    try:
        with path.open("wb") as fh:
            chunk = head
            while chunk:
                written += len(chunk)
                if written > max_bytes:
                    raise ValidationFailed(f"Video is too large. The upload limit is {max_bytes // (1024 * 1024)} MB.")
                fh.write(chunk)
                chunk = await upload.read(_CHUNK)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if written == 0:
        path.unlink(missing_ok=True)
        raise ValidationFailed("The uploaded file is empty")
    return relative, {"delivery": "upload", "mime_type": mime, "size_bytes": written, "original_filename": original}
