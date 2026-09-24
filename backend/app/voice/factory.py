from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.core.errors import ProviderUnavailable
from app.voice.base import SpeechToText, TextToSpeech
from app.voice.providers import (
    GeminiSpeechToText,
    HttpSpeechToText,
    HttpTextToSpeech,
    MockSpeechToText,
    MockTextToSpeech,
)


def _secret(value: object) -> str | None:
    return value.get_secret_value() if value is not None else None  # type: ignore[attr-defined]


@lru_cache
def get_stt() -> SpeechToText:
    s = get_settings()
    if s.stt_provider == "gemini":
        if not s.gemini_configured:
            raise ProviderUnavailable("STT_PROVIDER=gemini requires GEMINI_API_KEY", code="stt_not_configured")
        return GeminiSpeechToText(api_key=_secret(s.gemini_api_key) or "", model=s.gemini_model)
    if s.stt_provider == "mms":
        from app.voice.local_stt import MMSSpeechToText

        return MMSSpeechToText(model=s.stt_mms_model, device=s.translation_device)
    if s.stt_provider == "http":
        if not s.stt_http_url:
            raise ProviderUnavailable("STT_PROVIDER=http requires STT_HTTP_URL", code="stt_not_configured")
        return HttpSpeechToText(url=s.stt_http_url, api_key=_secret(s.stt_http_api_key))
    return MockSpeechToText()


@lru_cache
def get_tts() -> TextToSpeech:
    s = get_settings()
    if s.tts_provider == "http":
        if not s.tts_http_url:
            raise ProviderUnavailable("TTS_PROVIDER=http requires TTS_HTTP_URL", code="tts_not_configured")
        return HttpTextToSpeech(url=s.tts_http_url, api_key=_secret(s.tts_http_api_key), voice=s.tts_voice)
    return MockTextToSpeech()
