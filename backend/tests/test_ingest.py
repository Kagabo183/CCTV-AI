"""Link import building blocks: camera allowlist, credential stripping, real ffmpeg conversion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video import ffmpeg
from app.video.url_safety import UnsafeUrlError, UrlPolicy, check_url_syntax, strip_credentials

CAMERAS = UrlPolicy(camera_networks=("192.168.1.0/24",))


def test_camera_allowlist_opens_only_listed_networks() -> None:
    assert check_url_syntax("rtsp://admin:pw@192.168.1.64:554/Streaming/Channels/101", CAMERAS, streams=True) == "192.168.1.64"
    assert check_url_syntax("https://192.168.1.64:8443/ISAPI/stream.mjpg", CAMERAS, streams=True)  # camera web ports are fine
    for url in (
        "rtsp://192.168.2.10/stream",  # another LAN segment
        "rtsp://127.0.0.1/stream",  # the server itself
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/x.mp4",
    ):
        with pytest.raises(UnsafeUrlError):
            check_url_syntax(url, CAMERAS, streams=True)


def test_credentials_only_for_camera_streams() -> None:
    with pytest.raises(UnsafeUrlError):
        check_url_syntax("https://user:pw@videos.example.com/a.mp4", UrlPolicy(), streams=True)
    with pytest.raises(UnsafeUrlError):
        check_url_syntax("rtsp://cam.example.com/stream", UrlPolicy())  # rtsp needs streams=True
    assert check_url_syntax("rtsp://u:p@cam.example.com:8554/live", UrlPolicy(), streams=True) == "cam.example.com"


def test_strip_credentials() -> None:
    assert strip_credentials("rtsp://admin:s3cr3t@192.168.1.64:554/ch1?x=1") == "rtsp://192.168.1.64:554/ch1?x=1"
    assert strip_credentials("https://example.com/v.mp4") == "https://example.com/v.mp4"
    assert strip_credentials("rtsp://u:p@[fe80::1]:554/s") == "rtsp://[fe80::1]:554/s"


def _has_ffmpeg() -> bool:
    try:
        ffmpeg.ffmpeg_exe()
        return True
    except ffmpeg.FfmpegError:
        return False


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not available")
def test_normalize_converts_to_browser_playable_h264(tmp_path: Path) -> None:
    src = tmp_path / "camera_export.avi"  # MPEG-4 Part 2 in AVI: typical of old DVR exports, not playable in browsers
    subprocess.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=15:duration=2", "-c:v", "mpeg4", str(src)], check=True)
    before = ffmpeg.probe(src)
    assert before.vcodec == "mpeg4" and not before.browser_ready

    out = ffmpeg.normalize(src, tmp_path / "web.mp4", max_height=480)
    assert out.vcodec == "h264" and out.container == "mp4" and out.browser_ready
    assert out.height == 480 and out.width == 854  # downscaled, aspect kept
    assert out.duration and abs(out.duration - 2.0) < 0.3


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not available")
def test_probe_rejects_non_video(tmp_path: Path) -> None:
    bogus = tmp_path / "x.mp4"
    bogus.write_bytes(b"<html>not a video</html>")
    with pytest.raises(ffmpeg.FfmpegError):
        ffmpeg.probe(bogus)


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not available")
def test_variable_frame_rate_is_made_constant(tmp_path: Path) -> None:
    """Camera streams drop frames; stored video must be constant-rate or detection boxes drift in playback."""
    src = tmp_path / "vfr.mp4"
    # 40 frames at 10 fps, but frames 20+ are stamped 1 s late: a 1 s gap like a stalled camera.
    subprocess.run([
        ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4",
        "-vf", "setpts='(N/10+if(gte(N,20),1,0))/TB'", "-fps_mode", "passthrough", "-c:v", "libx264", str(src),
    ], check=True)
    info = ffmpeg.probe(src)
    assert ffmpeg.is_variable_rate(src, info)

    out = ffmpeg.normalize(src, tmp_path / "cfr.mp4")
    frames = ffmpeg.frame_count(tmp_path / "cfr.mp4")
    assert out.fps and out.duration and frames
    assert abs(frames / out.fps - out.duration) < 0.2  # frame index / fps now equals playback time
    assert not ffmpeg.is_variable_rate(tmp_path / "cfr.mp4", out)


def test_log_redaction_hides_stream_passwords() -> None:
    from app.core.logging import redact

    line = "capture failed for rtsp://admin:s3cr3t@192.168.1.64:554/ch1 and http://u:p@cam.local/mjpg"
    out = redact(line)
    assert "s3cr3t" not in out and ":p@" not in out
    assert "rtsp://[REDACTED]@192.168.1.64:554/ch1" in out
