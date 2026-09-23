"""Selects the VideoAnalyzer implementation from configuration.

This is the single place that knows concrete analyzers exist.

  "agent"  -> AgentVideoAnalyzer: tool-calling LLM over the local vision engine's
              events/tracks, escalating to GeminiVideoAnalyzer on a clip only
              when local evidence cannot answer (the hybrid design).
  "gemini" -> every question answered by Gemini on the video.
Future: a local VLM can replace Gemini as the escalation target, or as the agent LLM.
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
    if provider in ("gemini", "agent"):
        if not settings.gemini_configured:
            raise ProviderUnavailable(f"{provider} is selected but GEMINI_API_KEY is not set", code="analyzer_not_configured")
        from app.analyzers.gemini import GeminiVideoAnalyzer

        assert settings.gemini_api_key is not None
        gemini = GeminiVideoAnalyzer(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
            video_fps=settings.gemini_video_fps,
        )
        if provider == "gemini":
            return gemini
        from app.agent.analyzer import AgentVideoAnalyzer

        return AgentVideoAnalyzer(video_analyzer=gemini, model=settings.agent_model or settings.gemini_model, max_steps=settings.agent_max_steps)
    from app.analyzers.mock import MockVideoAnalyzer

    return MockVideoAnalyzer()
