"""The VideoAnalyzer interface and the normalized result model.

The rest of the application depends ONLY on the types in this module. A
provider (Gemini today; a local engine, local VLM or hybrid router later)
must translate its native output into `AnalysisResult`. No provider-specific
response shapes may leak past its implementation.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.events.types import EventType
from app.video.sources.base import MediaHandle, TimeWindow

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    timestamps: list[float] = Field(default_factory=list)


class SourceContext(BaseModel):
    """What the analyzer is told about the camera/video it is looking at."""

    source_id: uuid.UUID | None = None  # lets tool-using analyzers query stored vision data
    name: str
    location: str | None = None
    kind: str
    is_live: bool = False
    duration_seconds: float | None = None


class AnalysisQuery(BaseModel):
    question: str
    language: str = "rw"
    source: SourceContext
    history: list[ConversationTurn] = Field(default_factory=list)
    # Carried-over context: entities being discussed, focus times, prior events.
    context_notes: list[str] = Field(default_factory=list)
    window: TimeWindow | None = None  # restrict analysis to part of the media (seconds)
    user_id: uuid.UUID | None = None

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Normalized output
# ---------------------------------------------------------------------------


class TimeReference(BaseModel):
    start_seconds: float
    end_seconds: float | None = None
    label: str = ""


class DetectedEvent(BaseModel):
    event_type: EventType
    description: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    confidence: float | None = None
    evidence_level: str = "model_interpretation"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    description: str
    timestamp_seconds: float | None = None
    level: str | None = None  # detection | tracking | rule | model_interpretation


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class AnalysisResult(BaseModel):
    answer: str
    confidence: float = 0.0
    language: str = "rw"
    timestamps: list[TimeReference] = Field(default_factory=list)
    events: list[DetectedEvent] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    # Short descriptions of the things the answer talks about ("two people near
    # the gate", "white car"). Fed back into follow-up questions.
    referenced_entities: list[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
    analyzer: str
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)
    raw: dict[str, Any] | None = None  # provider payload, kept only for debugging
    trace: dict[str, Any] = Field(default_factory=dict)  # how the answer was produced (tools used, escalation)


# ---------------------------------------------------------------------------
# Analyzer contract
# ---------------------------------------------------------------------------


class PreparedVideo(BaseModel):
    """An analyzer-side handle to media (e.g. an uploaded Gemini file).

    Persisted on video_sessions.provider_ref so the video is prepared once
    and reused for every question until it expires.
    """

    analyzer: str
    ref: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None
    media_metadata: dict[str, Any] = Field(default_factory=dict)


class AnalyzerCapabilities(BaseModel):
    accepts_remote_uri: bool = False  # can fetch e.g. YouTube itself
    continuous_events: bool = False  # produces events without being asked (local engine)
    max_video_seconds: int | None = None


class VideoAnalyzer(ABC):
    name: str
    model: str | None = None
    capabilities: AnalyzerCapabilities = AnalyzerCapabilities()

    @abstractmethod
    async def prepare(self, media: MediaHandle) -> PreparedVideo:
        """Make media available to the analyzer. The caller releases `media` afterwards."""

    @abstractmethod
    async def analyze(self, prepared: PreparedVideo, query: AnalysisQuery) -> AnalysisResult:
        """Answer a question about prepared media, grounded in the video."""

    def is_valid(self, prepared: PreparedVideo, now: datetime) -> bool:
        return prepared.analyzer == self.name and (prepared.expires_at is None or prepared.expires_at > now)

    async def discard(self, prepared: PreparedVideo) -> None:
        """Delete provider-side copies. Optional."""
        return None
