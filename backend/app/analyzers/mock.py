"""Deterministic development analyzer, used when Gemini is not configured.

It never looks at the video. It exists so the whole pipeline (API, sessions,
conversation state, events, UI, voice) can be exercised without credentials.
Every answer is clearly marked as a mock. Refused in production.
"""

from __future__ import annotations

from app.analyzers.base import (
    AnalysisQuery,
    AnalysisResult,
    AnalyzerCapabilities,
    DetectedEvent,
    Evidence,
    PreparedVideo,
    TimeReference,
    VideoAnalyzer,
)
from app.events.types import EventType
from app.video.sources.base import MediaHandle


class MockVideoAnalyzer(VideoAnalyzer):
    name = "mock"
    model = "mock-v1"
    capabilities = AnalyzerCapabilities(accepts_remote_uri=True)

    async def prepare(self, media: MediaHandle) -> PreparedVideo:
        return PreparedVideo(analyzer=self.name, ref={"mock": True}, media_metadata=dict(media.metadata))

    async def analyze(self, prepared: PreparedVideo, query: AnalysisQuery) -> AnalysisResult:
        turn = sum(1 for t in query.history if t.role == "user") + 1
        where = query.source.location or query.source.name
        if query.language == "en":
            answer = (
                f"[DEV MOCK: Gemini is not configured] You asked: \"{query.question}\". "
                f"This is placeholder answer number {turn} for the {where} video."
            )
        else:
            answer = (
                f"[IGERAGEZA: Gemini ntirashyirwamo] Ikibazo cyawe: \"{query.question}\". "
                f"Iki ni igisubizo cy'igerageza cya {turn} kuri video ya {where}."
            )
        return AnalysisResult(
            answer=answer,
            confidence=0.0,
            language=query.language,
            timestamps=[TimeReference(start_seconds=3.0, end_seconds=8.0, label="mock segment")],
            events=[DetectedEvent(event_type=EventType.OBSERVATION, description="Mock observation (no real analysis)", start_seconds=3.0, end_seconds=8.0, confidence=0.0)],
            evidence=[Evidence(description="Mock analyzer: no video was analysed", timestamp_seconds=3.0)],
            referenced_entities=[f"mock entity from turn {turn}"],
            analyzer=self.name,
            model=self.model,
        )
