"""Conversation orchestrator: the heart of the platform.

    question (text, from typing or STT)
        -> resolve which camera/video the user means
        -> load conversation state + recent history
        -> ensure the video is prepared (VideoSessionService)
        -> VideoAnalyzer.analyze()        (interface; Gemini today)
        -> persist answer, timestamps, evidence, events, usage
        -> update rolling conversation state
    answer text (TTS happens outside, in the voice adapter)

It depends only on the VideoAnalyzer interface, never on a provider.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.video.sources.live import LIVE_KINDS
from app.analyzers.base import AnalysisQuery, AnalysisResult, SourceContext, VideoAnalyzer
from app.conversation.context import ConversationState, build_history
from app.core.config import get_settings
from app.core.errors import AppError, ProviderUnavailable, ValidationFailed
from app.models import AnalysisRequest, Conversation, ConversationMessage, VideoEvent, VideoSourceRecord
from app.services.video_sessions import VideoSessionService
from app.video.gateway import VideoGateway

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 2000


@dataclass
class TurnResult:
    user_message: ConversationMessage
    assistant_message: ConversationMessage
    switched_to_source: VideoSourceRecord | None = None


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def resolve_source_mention(question: str, current: VideoSourceRecord | None, candidates: list[VideoSourceRecord]) -> VideoSourceRecord | None:
    """If the user names a different camera ("... camera yo ku irembo"), return it.

    Deliberately conservative: switch only when exactly one other source's
    location or name appears in the question and the current one does not.
    """
    q = f" {_normalize(question)} "

    def mentioned(src: VideoSourceRecord) -> bool:
        labels = [src.location or "", src.name]
        return any(len(n := _normalize(label).strip()) >= 3 and f" {n} " in q for label in labels)

    if current is not None and mentioned(current):
        return None
    matches = [s for s in candidates if (current is None or s.id != current.id) and mentioned(s)]
    return matches[0] if len(matches) == 1 else None


class ConversationOrchestrator:
    def __init__(self, db: AsyncSession, gateway: VideoGateway, analyzer: VideoAnalyzer) -> None:
        self.db = db
        self.analyzer = analyzer
        self.sessions = VideoSessionService(db, gateway, analyzer)
        self.settings = get_settings()

    async def ask(self, *, conversation: Conversation, question: str, input_mode: str = "text") -> TurnResult:
        question = question.strip()
        if not question:
            raise ValidationFailed("The question is empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValidationFailed(f"Questions are limited to {MAX_QUESTION_CHARS} characters")

        source, switched = await self._resolve_source(conversation, question)

        user_message = ConversationMessage(
            conversation_id=conversation.id, role="user", content=question, language=conversation.language, input_mode=input_mode, video_source_id=source.id
        )
        self.db.add(user_message)
        if not conversation.title:
            conversation.title = question[:120]
        await self.db.commit()

        history = build_history(await self._messages(conversation.id, exclude=user_message.id))
        state = ConversationState.load(conversation.context)
        query = AnalysisQuery(
            question=question,
            language=conversation.language,
            user_id=conversation.user_id,
            source=SourceContext(
                source_id=source.id,
                name=source.name,
                location=source.location,
                kind=source.kind,
                is_live=source.kind in LIVE_KINDS,
                duration_seconds=None if source.kind in LIVE_KINDS else source.source_metadata.get("duration_seconds"),
            ),
            history=history,
            context_notes=state.notes(),
        )

        try:
            result, request = await self._analyze(conversation, source, query)
        except AppError as exc:
            user_message.message_metadata = {"failed": True, "error": exc.code}
            await self.db.commit()
            raise

        assistant_message = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=result.answer,
            language=result.language,
            video_source_id=source.id,
            analysis_request_id=request.id,
            confidence=result.confidence,
            timestamps=[t.model_dump() for t in result.timestamps],
            evidence=[e.model_dump() for e in result.evidence],
            message_metadata={
                "analyzer": result.analyzer,
                "model": result.model,
                "insufficient_evidence": result.insufficient_evidence,
                "events": [e.model_dump(mode="json") for e in result.events],
                **result.trace,
            },
        )
        self.db.add(assistant_message)
        state.update(result)
        conversation.context = state.model_dump()
        conversation.updated_at = datetime.now(UTC)
        await self.db.commit()
        return TurnResult(user_message=user_message, assistant_message=assistant_message, switched_to_source=source if switched else None)

    # -- internals -------------------------------------------------------------

    async def _resolve_source(self, conversation: Conversation, question: str) -> tuple[VideoSourceRecord, bool]:
        current = await self.db.get(VideoSourceRecord, conversation.video_source_id) if conversation.video_source_id else None
        candidates = (
            await self.db.execute(select(VideoSourceRecord).where(VideoSourceRecord.owner_id == conversation.user_id, VideoSourceRecord.status == "ready"))
        ).scalars().all()
        mentioned = resolve_source_mention(question, current, list(candidates))
        if mentioned is not None:
            conversation.video_source_id = mentioned.id
            conversation.video_session_id = None
            return mentioned, True
        if current is None:
            raise ValidationFailed("Select a camera or video before asking a question", code="no_source")
        if current.status != "ready":
            raise ValidationFailed("This video source is not ready", code="source_not_ready")
        return current, False

    async def _messages(self, conversation_id: uuid.UUID, exclude: uuid.UUID) -> list[ConversationMessage]:
        rows = await self.db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id, ConversationMessage.id != exclude)
            .order_by(ConversationMessage.created_at)
        )
        return list(rows.scalars().all())

    async def _analyze(self, conversation: Conversation, source: VideoSourceRecord, query: AnalysisQuery) -> tuple[AnalysisResult, AnalysisRequest]:
        session = await self.sessions.open(source, conversation.user_id)
        conversation.video_session_id = session.id
        session, prepared = await self.sessions.ensure_ready(session.id)

        request = AnalysisRequest(
            user_id=conversation.user_id,
            video_source_id=source.id,
            video_session_id=session.id,
            conversation_id=conversation.id,
            analyzer=self.analyzer.name,
            model=self.analyzer.model,
            question=query.question,
        )
        self.db.add(request)
        await self.db.commit()

        started = time.perf_counter()
        try:
            try:
                result = await self.analyzer.analyze(prepared, query)
            except ProviderUnavailable as exc:
                # A reused provider handle may have expired server-side: re-prepare once.
                if exc.code != "provider_bad_request":
                    raise
                logger.info("Analyzer rejected cached media; re-preparing session %s", session.id)
                await self.sessions.invalidate(session)
                session, prepared = await self.sessions.ensure_ready(session.id)
                result = await self.analyzer.analyze(prepared, query)
        except AppError as exc:
            request.status = "failed"
            request.error_code = exc.code
            request.latency_ms = int((time.perf_counter() - started) * 1000)
            request.completed_at = datetime.now(UTC)
            await self.db.commit()
            raise

        request.status = "completed"
        request.latency_ms = int((time.perf_counter() - started) * 1000)
        request.input_tokens = result.usage.input_tokens
        request.output_tokens = result.usage.output_tokens
        request.completed_at = datetime.now(UTC)
        if self.settings.store_raw_provider_responses:
            request.raw_response = result.raw
        for event in result.events:
            self.db.add(
                VideoEvent(
                    video_source_id=source.id,
                    video_session_id=session.id,
                    analysis_request_id=request.id,
                    event_type=event.event_type.value,
                    evidence_level=event.evidence_level,
                    description=event.description,
                    start_time=event.start_seconds,
                    end_time=event.end_seconds,
                    confidence=event.confidence,
                    detector=result.analyzer,
                    event_metadata=event.metadata,
                )
            )
        await self.db.commit()
        return result, request
