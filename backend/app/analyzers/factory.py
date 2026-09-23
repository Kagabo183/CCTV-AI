"""Selects the VideoAnalyzer implementation from configuration.

This is the single place that knows concrete analyzers exist. Future:
  "local"  -> LocalVideoAnalyzer (YOLO/RT-DETR + tracker + event engine)
  "hybrid" -> HybridVideoAnalyzer(local=..., reasoning=GeminiVideoAnalyzer)
              answers simple questions from locally stored events and
              escalates hard visual reasoning to Gemini.
"""

from __future__ import annotations

from functools import lru_cache

from app.analyzers.base import VideoAnalyzer
from app.core.config import get_settings
from app.core.errors import ProviderUnavailable


@lru_cache
def get_video_analyzer() -> VideoAnalyzer:
    settings = get_settings()
    provider = settings.resolved_analyzer_provider
    if provider == "gemini":
        if not settings.gemini_configured:
            raise ProviderUnavailable("Gemini is selected but GEMINI_API_KEY is not set", code="analyzer_not_configured")
        from app.analyzers.gemini import GeminiVideoAnalyzer

        assert settings.gemini_api_key is not None
        return GeminiVideoAnalyzer(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
            video_fps=settings.gemini_video_fps,
        )
    from app.analyzers.mock import MockVideoAnalyzer

    return MockVideoAnalyzer()
