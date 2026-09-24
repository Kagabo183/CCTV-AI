"""Open-ended visual understanding, beyond the detector's fixed class list.

Detectors (YOLO/RT-DETR, COCO or Objects365) only know their vocabulary and can
confidently mislabel things outside it. For "what is that object?", "what is
the animal doing?", "what changed?", the agent sends the relevant clip or
frames to a vision-language model through this interface:

    VideoUnderstandingProvider
        ├── GeminiVideoProvider      whole clip (Gemini Files API; video + audio)
        ├── LocalVLMProvider         sampled frames -> local VLM (Ollama / LM Studio / vLLM, OpenAI-compatible)
        └── TogetherVideoProvider    sampled frames -> Together AI vision models (OpenAI-compatible)

All results are evidence level "model_interpretation": a model's description,
never presented as a measured fact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from app.analyzers.base import DetectedEvent, PreparedVideo, SourceContext, TimeReference, Usage


@dataclass
class VideoRef:
    """Where the video can be read from. Providers use what they support."""

    prepared: PreparedVideo | None = None  # provider-side upload (e.g. Gemini file)
    local_path: Path | str | None = None  # stored MP4, or the direct stream URL of an online video, for frame-based providers
    duration_seconds: float | None = None


class UnderstandingRequest(BaseModel):
    question: str
    source: SourceContext
    start_seconds: float | None = None
    end_seconds: float | None = None
    language: str = "en"


class UnderstandingResult(BaseModel):
    description: str
    observations: list[str] = Field(default_factory=list)
    timestamps: list[TimeReference] = Field(default_factory=list)
    events: list[DetectedEvent] = Field(default_factory=list)
    confidence: float | None = None
    insufficient_evidence: bool = False
    provider: str
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)


class VideoUnderstandingProvider(ABC):
    name: str
    model: str | None = None

    @abstractmethod
    async def understand(self, video: VideoRef, request: UnderstandingRequest) -> UnderstandingResult:
        """Describe what the video (or the requested time window) shows."""
