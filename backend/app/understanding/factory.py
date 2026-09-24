"""Selects the video-understanding provider(s) from configuration.

VIDEO_UNDERSTANDING_PROVIDERS is an ordered list; later entries are fallbacks,
e.g. "gemini,local_vlm" = Gemini, and the local VLM when Gemini is unavailable.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.core.errors import ProviderUnavailable
from app.understanding.base import VideoUnderstandingProvider
from app.understanding.providers import FallbackProvider, GeminiVideoProvider, LocalVLMProvider, TogetherVideoProvider


def _build(name: str) -> VideoUnderstandingProvider | None:
    s = get_settings()
    if name == "gemini":
        if not s.gemini_configured:
            return None
        from app.analyzers.gemini import GeminiVideoAnalyzer

        assert s.gemini_api_key is not None
        return GeminiVideoProvider(GeminiVideoAnalyzer(api_key=s.gemini_api_key.get_secret_value(), model=s.gemini_model, timeout_seconds=s.gemini_timeout_seconds))
    if name == "local_vlm":
        return LocalVLMProvider(base_url=s.local_vlm_url, model=s.local_vlm_model, frames=s.understanding_frames, reasoning_effort=s.local_vlm_reasoning) if s.local_vlm_model else None
    if name == "together":
        if s.together_api_key is None:
            return None
        return TogetherVideoProvider(api_key=s.together_api_key.get_secret_value(), model=s.together_vlm_model, frames=s.understanding_frames)
    raise ValueError(f"Unknown video understanding provider {name!r}")


@lru_cache
def get_understanding_provider() -> VideoUnderstandingProvider:
    providers = [p for p in (_build(n) for n in get_settings().video_understanding_providers) if p is not None]
    if not providers:
        raise ProviderUnavailable("No video understanding provider is configured", code="understanding_not_configured")
    return providers[0] if len(providers) == 1 else FallbackProvider(providers)
