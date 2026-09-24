"""ffmpeg helpers: probe, normalise to browser-playable MP4, capture live clips.

Uses a system ffmpeg if installed, otherwise the static build bundled with the
`imageio-ffmpeg` package (includes libx264 and NVIDIA NVENC).

Everything the platform stores ends up as H.264 + AAC in MP4 with the index at
the front (faststart). Browsers play it, OpenCV decodes it, and Gemini accepts it,
whatever the source codec (HEVC CCTV exports, AV1 from YouTube, MJPEG, ...).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


class FfmpegError(RuntimeError):
    pass


@lru_cache
def ffmpeg_exe() -> str:
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError) as exc:  # pragma: no cover
        raise FfmpegError("ffmpeg is not available. Install it or `pip install imageio-ffmpeg`.") from exc
    # The bundled binary has a versioned name (ffmpeg-win-x86_64-v7.1.exe); tools like
    # yt-dlp look for "ffmpeg(.exe)", so expose it under the standard name next to it.
    standard = bundled.with_name("ffmpeg.exe" if bundled.suffix == ".exe" else "ffmpeg")
    if not standard.exists():
        try:
            standard.hardlink_to(bundled)
        except OSError:
            shutil.copy2(bundled, standard)
    # Some yt-dlp checks (partial downloads) only search PATH, ignoring ffmpeg_location.
    directory = str(standard.parent)
    if directory not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    return str(standard)


@lru_cache
def nvenc_available() -> bool:
    """True if this ffmpeg has NVENC and an NVIDIA GPU accepts an encode."""
    try:
        run = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
            timeout=30,
        )
        return run.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass
class MediaInfo:
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    vcodec: str | None
    acodec: str | None
    container: str
    base_fps: float | None = None  # "tbr": the stream's nominal rate; differs from fps (average) when variable

    def as_metadata(self) -> dict[str, object]:
        return {
            "duration_seconds": round(self.duration, 2) if self.duration else None,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "codec": self.vcodec,
            "audio_codec": self.acodec,
        }

    @property
    def browser_ready(self) -> bool:
        return self.vcodec == "h264" and self.container == "mp4" and self.acodec in (None, "aac", "mp3")


_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\S+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"(\d+(?:\.\d+)?) fps")
_TBR = re.compile(r"(\d+(?:\.\d+)?)k? tbr")
_AUDIO = re.compile(r"Stream #\S+.*?: Audio: (\w+)")


def probe(path: Path) -> MediaInfo:
    """Read codec/size/duration by parsing `ffmpeg -i` (the bundled build has no ffprobe)."""
    run = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace", timeout=60)
    text = run.stderr
    video = _VIDEO.search(text)
    if video is None:
        raise FfmpegError("No video stream found in the file")
    audio = _AUDIO.search(text)
    duration = _DURATION.search(text)
    video_line = text[video.start():text.find("\n", video.start())]
    fps = _FPS.search(video_line)
    tbr = _TBR.search(video_line)
    return MediaInfo(
        duration=(int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])) if duration else None,
        width=int(video[2]),
        height=int(video[3]),
        fps=float(fps[1]) if fps else None,
        vcodec=video[1],
        acodec=audio[1] if audio else None,
        container=path.suffix.lower().lstrip("."),
        base_fps=float(tbr[1]) if tbr else None,
    )


def _video_encoder(max_height: int, height: int | None, fps: float | None = None) -> list[str]:
    """H.264 at a CONSTANT frame rate.

    Constant frame rate matters: local analysis times frames as index / fps, while
    browsers play by timestamp. Variable-rate video (common from cameras) would
    make detection boxes drift away from the picture.
    """
    scale = ["-vf", f"scale=-2:{max_height}"] if height and height > max_height else []
    rate = ["-fps_mode", "cfr", *(["-r", f"{fps:g}"] if fps else [])]
    if nvenc_available():
        return [*scale, *rate, "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-pix_fmt", "yuv420p"]
    return [*scale, *rate, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]


_FRAMES = re.compile(r"frame=\s*(\d+)")


def frame_count(path: Path) -> int | None:
    """Count video frames by remuxing to null (fast: no decoding)."""
    run = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"], capture_output=True, text=True, errors="replace", timeout=120)
    found = _FRAMES.findall(run.stderr)
    return int(found[-1]) if found else None


def is_variable_rate(path: Path, info: MediaInfo) -> bool:
    """True when frames are not evenly spaced in time.

    Two signals: the average rate differs from the nominal rate (tbr), or the
    frame count does not fit the duration (dropped/stalled frames in a capture).
    """
    if not (info.fps and info.duration):
        return False
    if info.base_fps and abs(info.fps - info.base_fps) > 0.02 * info.base_fps:
        return True
    frames = frame_count(path)
    if not frames:
        return False
    return abs(frames / info.fps - info.duration) > max(0.5, 0.03 * info.duration)


def _run(args: list[str], timeout: float) -> None:
    try:
        run = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError("Video processing took too long") from exc
    if run.returncode != 0:
        detail = (run.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise FfmpegError(f"ffmpeg failed: {detail[0][:300]}")


def normalize(src: Path, dst: Path, *, max_height: int = 1080, max_seconds: float | None = None) -> MediaInfo:
    """Write a browser-playable H.264/AAC MP4. Copies the video stream when it already is one."""
    info = probe(src)
    limit = ["-t", str(max_seconds)] if max_seconds else []
    copy_video = info.vcodec == "h264" and (info.height or 0) <= max_height and not is_variable_rate(src, info)
    video = ["-c:v", "copy"] if copy_video else _video_encoder(max_height, info.height, info.fps if info.fps and info.fps <= 60 else None)
    audio = ["-c:a", "copy"] if info.acodec == "aac" else ["-c:a", "aac", "-b:a", "128k"]
    timeout = max(120.0, (info.duration or 600) * (0.5 if copy_video else 3))
    _run(["-i", str(src), *limit, "-map", "0:v:0", "-map", "0:a:0?", *video, *audio, "-movflags", "+faststart", str(dst)], timeout)
    return probe(dst)


def capture(url: str, dst: Path, *, seconds: float, max_height: int = 1080, wallclock: bool = False, fps: float | None = None) -> MediaInfo:
    """Record `seconds` of a live stream (RTSP, HLS, MJPEG, ...) into a constant-frame-rate MP4.

    wallclock=True stamps frames with their arrival time: needed for MJPEG, which
    carries no timestamps (ffmpeg would otherwise assume 25 fps and speed the clip up).
    """
    is_rtsp = url.lower().startswith(("rtsp://", "rtsps://"))
    network = ["-rtsp_transport", "tcp", "-timeout", "15000000"] if is_rtsp else ["-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1"]
    timing = ["-use_wallclock_as_timestamps", "1"] if wallclock else []
    # Never follow the stream into other protocols (e.g. file://) from inside a playlist.
    whitelist = ["-protocol_whitelist", "tcp,tls,http,https,rtsp,rtsps,rtp,udp,crypto,hls"]
    _run(
        [*whitelist, *network, *timing, "-i", url, "-t", str(seconds), "-map", "0:v:0", "-map", "0:a:0?", *_video_encoder(max_height, None, fps), "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)],
        timeout=seconds + 90,
    )
    return probe(dst)
