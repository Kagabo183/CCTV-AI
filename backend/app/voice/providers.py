"""Speech provider implementations.

mock   - development stand-ins, clearly labelled, no external calls
gemini - STT via Gemini audio understanding (reuses GEMINI_API_KEY)
http   - generic contract for a self-hosted Kinyarwanda model server, e.g.
         a Whisper/MMS fine-tune for STT and an MMS/Coqui voice for TTS.
         See docs/VOICE.md for the request/response contract.
"""

from __future__ import annotations

import logging

import httpx

from app.core.errors import ProviderUnavailable
from app.voice.base import SpeechToText, SynthesizedSpeech, TextToSpeech, Transcript

logger = logging.getLogger(__name__)


class MockSpeechToText(SpeechToText):
    """Returns a fixed Kinyarwanda question so the voice flow can be demoed."""

    name = "mock"
    is_placeholder = True
    PHRASE = "Ni iki kiri kuba kuri iyi video?"

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "rw") -> Transcript:
        return Transcript(text=self.PHRASE, language=language, confidence=0.0, provider=self.name)


class MockTextToSpeech(TextToSpeech):
    """No speech output: the UI shows that Kinyarwanda TTS is not configured."""

    name = "mock"
    is_placeholder = True

    async def synthesize(self, text: str, language: str = "rw") -> SynthesizedSpeech | None:
        return None


class GeminiSpeechToText(SpeechToText):
    name = "gemini"

    def __init__(self, *, api_key: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "rw") -> Transcript:
        from google.genai import errors, types

        language_name = {"rw": "Kinyarwanda", "en": "English"}.get(language, language)
        prompt = (
            f"Transcribe this speech exactly as spoken. The speaker most likely uses {language_name}, "
            "possibly mixed with English or French words. Output only the transcript text, nothing else. "
            "If there is no intelligible speech, output an empty string."
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=[types.Part.from_bytes(data=audio, mime_type=mime_type.split(";")[0]), prompt],
                config=types.GenerateContentConfig(temperature=0.0),
            )
        except errors.APIError as exc:
            raise ProviderUnavailable("Speech recognition failed", code="stt_error") from exc
        return Transcript(text=(response.text or "").strip().strip('"'), language=language, provider=self.name)


class HttpSpeechToText(SpeechToText):
    name = "http"

    def __init__(self, *, url: str, api_key: str | None) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "rw") -> Transcript:
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                response = await client.post(self._url, headers=self._headers, data={"language": language}, files={"audio": ("speech", audio, mime_type)})
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("STT provider error: %s", type(exc).__name__)
            raise ProviderUnavailable("Speech recognition service is unavailable", code="stt_error") from exc
        return Transcript(text=str(data.get("text", "")).strip(), language=data.get("language", language), confidence=data.get("confidence"), provider=self.name)


class HttpTextToSpeech(TextToSpeech):
    name = "http"

    def __init__(self, *, url: str, api_key: str | None, voice: str | None) -> None:
        self._url = url
        self._voice = voice
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def synthesize(self, text: str, language: str = "rw") -> SynthesizedSpeech | None:
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                response = await client.post(self._url, headers=self._headers, json={"text": text, "language": language, "voice": self._voice})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("TTS provider error: %s", type(exc).__name__)
            raise ProviderUnavailable("Speech synthesis service is unavailable", code="tts_error") from exc
        mime = response.headers.get("content-type", "audio/wav").split(";")[0]
        if not mime.startswith("audio/"):
            raise ProviderUnavailable("Speech synthesis service returned no audio", code="tts_error")
        return SynthesizedSpeech(audio=response.content, mime_type=mime, provider=self.name)
