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
    video_max_download_mb: int = 500
    enable_video_uploads: bool = True
    video_max_upload_mb: int = 500
    upload_dir: Path = Path("./uploads")
    enable_local_video_sources: bool = False
    local_video_dir: Path = Path("./sample_videos")
    video_work_dir: Path = Path("./.video_work")

    # Local vision engine (detector -> tracker -> event engine). See docs/VISION_BENCHMARK.md.
    vision_enabled: bool = True
    vision_detector: Literal["yolo", "rtdetr"] = "yolo"
    vision_weights: str | None = None  # default per detector: yolo26s.pt / rtdetr-l.pt
    vision_tracker: Literal["bytetrack", "botsort"] = "bytetrack"
    vision_device: str = "auto"  # auto | cuda:0 | cpu
    vision_sample_fps: float = 10.0
    vision_confidence: float = 0.25
    vision_weights_dir: Path = Path("./models")
    vision_artifacts_dir: Path = Path("./vision_artifacts")  # per-run box tracks for the UI overlay

    # Conversational agent (tool-calling LLM over local event memory; escalates to Gemini video)
    agent_model: str | None = None  # defaults to GEMINI_MODEL
    agent_max_steps: int = 6

    # Voice
    stt_provider: Literal["mock", "gemini", "http"] = "mock"
    tts_provider: Literal["mock", "http"] = "mock"
    stt_http_url: str | None = None
    stt_http_api_key: SecretStr | None = None
    tts_http_url: str | None = None
    tts_http_api_key: SecretStr | None = None
    tts_voice: str | None = None

    default_language: str = "rw"
    ask_rate_limit_per_minute: int = 20
    store_raw_provider_responses: bool = False

    @field_validator("cors_origins", "video_url_allowed_domains", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("gemini_api_key", "stt_http_api_key", "tts_http_api_key", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        return None if value in ("", None) else value

    @field_validator("gemini_video_fps", "stt_http_url", "tts_http_url", "tts_voice", "vision_weights", "agent_model", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def gemini_configured(self) -> bool:
        return self.gemini_api_key is not None and bool(self.gemini_api_key.get_secret_value())

    @property
    def resolved_analyzer_provider(self) -> Literal["agent", "gemini", "mock"]:
        if self.video_analyzer_provider == "auto":
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
