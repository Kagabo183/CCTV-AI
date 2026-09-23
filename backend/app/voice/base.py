"""Speech interfaces. Kept separate from video analysis on purpose: the
conversation orchestrator only sees text; voice is an input/output adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/aac", "audio/flac"}
MAX_AUDIO_BYTES = 10 * 1024 * 1024


class Transcript(BaseModel):
    text: str
    language: str
    confidence: float | None = None
    provider: str


class SynthesizedSpeech(BaseModel):
    audio: bytes
    mime_type: str
    provider: str


class SpeechToText(ABC):
    name: str
    is_placeholder: bool = False

    @abstractmethod
    async def transcribe(self, audio: bytes, mime_type: str, language: str = "rw") -> Transcript: ...


class TextToSpeech(ABC):
    name: str
    is_placeholder: bool = False

    @abstractmethod
    async def synthesize(self, text: str, language: str = "rw") -> SynthesizedSpeech | None:
        """Return audio, or None when the provider cannot produce speech."""
