"""Application configuration.

All secrets (Gemini key, speech provider keys, JWT secret) are read from the
environment on the server only. Nothing in here is ever sent to the frontend.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    secret_key: SecretStr = SecretStr("change-me")
    access_token_ttl_minutes: int = 720
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    database_url: str = "postgresql+asyncpg://visionary:visionary@localhost:5432/visionary"
    redis_url: str | None = "redis://localhost:6379/0"

    # Video analyzer
    # auto -> agent (local vision + Gemini escalation) when Gemini is configured and vision is enabled,
    #         gemini when only Gemini is configured, otherwise mock.
    video_analyzer_provider: Literal["auto", "agent", "gemini", "mock"] = "auto"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.6-flash"
    gemini_timeout_seconds: int = 180
    gemini_video_fps: float | None = None

    # Video sources
    video_url_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    video_url_allow_http: bool = False
    # Private networks hosting your own cameras/NVRs, e.g. "192.168.1.0/24,10.0.5.0/24".
    # Only these private addresses are reachable (RTSP, MJPEG, camera web pages).
    camera_private_networks: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Imported links (YouTube, web pages, HLS, RTSP/MJPEG clips) are stored as H.264 MP4.
    video_import_max_seconds: int = 1800  # longer videos are cut
    video_import_max_height: int = 1080  # larger videos are downscaled when re-encoded
    # YouTube sometimes answers automated requests with "Sign in to confirm you're not a bot".
    # A Netscape cookies.txt exported from a signed-in browser avoids it (keep it private).
    ytdlp_cookies_file: Path | None = None
    live_clip_seconds: int = 60  # default length recorded from live streams (max 600)
    video_max_download_mb: int = 500
    enable_video_uploads: bool = True
    video_max_upload_mb: int = 500
    upload_dir: Path = Path("./uploads")
    enable_local_video_sources: bool = False
    local_video_dir: Path = Path("./sample_videos")
    video_work_dir: Path = Path("./.video_work")

    # Local vision engine (detector -> tracker -> event engine). See docs/VISION_BENCHMARK.md.
    vision_enabled: bool = True
    vision_detector: Literal["yolo", "yolo_o365", "rtdetr"] = "yolo"
    vision_weights: str | None = None  # default per detector: yolo26s.pt / yolo26s-objv1-150.pt / rtdetr-l.pt
    # Restrict detection to these class names (comma-separated). Empty = every class the model knows.
    vision_classes: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Tracks whose mean confidence is below this (or whose class keeps changing) are labelled "unknown".
    vision_confirm_confidence: float = 0.5
    vision_tracker: Literal["bytetrack", "botsort"] = "bytetrack"
    vision_device: str = "auto"  # auto | cuda:0 | cpu
    vision_sample_fps: float = 10.0
    # Long videos are sampled more sparsely so one analysis stays within this many frames
    # (e.g. a 30-minute documentary at ~1.7 fps instead of 10). "At the same time" counts stay valid.
    vision_max_frames: int = 3000
    vision_confidence: float = 0.25
    # Full-frame inference size. Larger finds smaller objects but costs time (see docs/SMALL_OBJECT_BENCHMARK.md).
    vision_image_size: int = 1280
    # Sliced (SAHI-style) inference for small or distant objects: off | on.
    vision_tiling: Literal["off", "on"] = "on"
    vision_tile_size: int = 0  # tile edge in source pixels; 0 = auto (45% of the frame's short side)
    vision_tile_overlap: float = 0.25  # fraction of the tile shared with its neighbour
    vision_tile_image_size: int = 640  # each tile is run at this size (> tile size = upscaled)
    vision_tile_full_frame: bool = True  # also run one full-frame pass (large objects) and merge
    # Classes taken from tiles (others only from the full-frame pass). Empty = all.
    vision_tile_classes: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["person", "bicycle", "car", "motorcycle", "bus", "truck"])
    vision_weights_dir: Path = Path("./models")
    vision_artifacts_dir: Path = Path("./vision_artifacts")  # per-run box tracks for the UI overlay

    # Open-ended visual understanding (vision-language models), in fallback order: gemini, local_vlm, together
    video_understanding_providers: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["gemini", "local_vlm"])
    local_vlm_url: str = "http://127.0.0.1:11434/v1"  # Ollama; LM Studio: http://127.0.0.1:1234/v1
    local_vlm_model: str | None = None  # e.g. qwen3-vl:8b (empty = disabled)
    local_vlm_reasoning: str | None = None  # "none" = faster but less grounded; empty = model default
    together_api_key: SecretStr | None = None
    together_vlm_model: str = "Qwen/Qwen2.5-VL-72B-Instruct"
    understanding_frames: int = 6  # frames sampled per question for frame-based providers

    # Conversational agent (tool-calling LLM over local event memory; escalates to Gemini video)
    agent_model: str | None = None  # defaults to GEMINI_MODEL
    # gemini = Gemini drives the tools | local = an OpenAI-compatible server (Ollama/LM Studio/vLLM), e.g. Qwen3
    agent_llm: Literal["gemini", "local"] = "gemini"
    agent_local_url: str | None = None  # default: LOCAL_VLM_URL
    agent_local_model: str | None = None  # default: LOCAL_VLM_MODEL
    agent_local_reasoning: str | None = None  # "none" = faster, less careful
    # Local translation (NLLB-200): the local agent works in English, questions/answers are translated.
    translation_provider: Literal["nllb", "none"] = "nllb"
    nllb_model: str = "facebook/nllb-200-distilled-1.3B"
    translation_device: str = "auto"
    agent_max_steps: int = 6

    # Voice
    stt_provider: Literal["mock", "gemini", "http", "mms"] = "mock"
    stt_mms_model: str = "facebook/mms-1b-all"  # local Kinyarwanda/English speech recognition
    tts_provider: Literal["mock", "http"] = "mock"
    stt_http_url: str | None = None
    stt_http_api_key: SecretStr | None = None
    tts_http_url: str | None = None
    tts_http_api_key: SecretStr | None = None
    tts_voice: str | None = None

    default_language: str = "rw"
    ask_rate_limit_per_minute: int = 20
    store_raw_provider_responses: bool = False

    @field_validator("cors_origins", "video_url_allowed_domains", "camera_private_networks", "vision_classes", "vision_tile_classes", "video_understanding_providers", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("gemini_api_key", "stt_http_api_key", "tts_http_api_key", "together_api_key", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        return None if value in ("", None) else value

    @field_validator("gemini_video_fps", "stt_http_url", "tts_http_url", "tts_voice", "vision_weights", "agent_model", "local_vlm_model", "local_vlm_reasoning", "agent_local_url", "agent_local_model", "agent_local_reasoning", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def gemini_configured(self) -> bool:
        return self.gemini_api_key is not None and bool(self.gemini_api_key.get_secret_value())

    @property
    def resolved_analyzer_provider(self) -> Literal["agent", "gemini", "mock"]:
        if self.video_analyzer_provider == "auto":
            if self.agent_llm == "local" and self.vision_enabled:
                return "agent"  # fully local: no Gemini key needed
            if not self.gemini_configured:
                return "mock"
            return "agent" if self.vision_enabled else "gemini"
        return self.video_analyzer_provider

    @property
    def max_upload_bytes(self) -> int:
        return self.video_max_upload_mb * 1024 * 1024

    @property
    def max_download_bytes(self) -> int:
        return self.video_max_download_mb * 1024 * 1024

    def assert_production_safe(self) -> None:
        if self.environment != "production":
            return
        if self.secret_key.get_secret_value() in ("", "change-me") or len(self.secret_key.get_secret_value()) < 32:
            raise RuntimeError("SECRET_KEY must be set to a strong random value in production")
        if self.resolved_analyzer_provider == "mock":
            raise RuntimeError("The mock video analyzer cannot be used in production")
        if self.enable_local_video_sources:
            raise RuntimeError("Local video sources are a development feature and cannot be enabled in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
