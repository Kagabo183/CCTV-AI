"""Gemini implementation of VideoAnalyzer.

Configuration (server-side only): GEMINI_API_KEY, GEMINI_MODEL,
GEMINI_TIMEOUT_SECONDS, GEMINI_VIDEO_FPS.

Flow:
  prepare(): upload the media file to the Gemini Files API once (or pass a
             YouTube URL through) and return a PreparedVideo with the file
             URI. Files expire after ~48 h, tracked via expires_at.
  analyze(): send [video + source context] + conversation history + the
             question, request JSON matching `_GeminiAnswer`, then normalize
             into AnalysisResult. `_GeminiAnswer` is private to this module.

The video part is always placed first in the prompt so repeated questions
about the same video share a prefix, which lets Gemini's implicit context
caching reduce cost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.analyzers.base import (
    AnalysisQuery,
    AnalysisResult,
    AnalyzerCapabilities,
    DetectedEvent,
    Evidence,
    PreparedVideo,
    TimeReference,
    Usage,
    VideoAnalyzer,
)
from app.core.errors import ProviderUnavailable
from app.events.types import EventType, normalize_event_type
from app.video.sources.base import MediaHandle

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {"rw": "Kinyarwanda", "en": "English", "fr": "French", "sw": "Swahili"}
_FILE_TTL = timedelta(hours=46)  # Gemini keeps uploaded files 48 h; leave margin
_TRANSIENT_CODES = {500, 502, 503, 504}  # Gemini overload/outage: worth retrying
_RETRY_DELAYS = (1.5, 4.0)


# --- Provider-private response schema --------------------------------------


class _GeminiTime(BaseModel):
    start_seconds: float
    end_seconds: float | None = None
    label: str = ""


class _GeminiEvent(BaseModel):
    event_type: str
    description: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    confidence: float | None = None


class _GeminiEvidence(BaseModel):
    description: str
    timestamp_seconds: float | None = None


class _GeminiAnswer(BaseModel):
    answer: str = Field(description="Natural spoken answer in the requested language. No markdown.")
    confidence: float = Field(description="0.0-1.0: how well the video supports the answer")
    insufficient_evidence: bool = Field(description="True if the video does not show enough to answer")
    timestamps: list[_GeminiTime] = Field(description="Moments in the video relevant to the answer, in seconds from the start")
    events: list[_GeminiEvent] = Field(description="Discrete events observed that relate to the question")
    evidence: list[_GeminiEvidence] = Field(description="Concrete visual observations supporting the answer")
    referenced_entities: list[str] = Field(description="Short English descriptions of the people/vehicles/objects the answer refers to, e.g. 'man in red jacket near gate'")


def _system_instruction(query: AnalysisQuery) -> str:
    language = LANGUAGE_NAMES.get(query.language, query.language)
    event_types = ", ".join(e.value for e in EventType)
    return f"""You are the video analysis engine of a CCTV assistant used in Rwanda. Security staff ask questions about the video from a camera and you answer from what the video shows.

Rules:
- Answer ONLY from what is visible or audible in the provided video. Never invent people, vehicles, objects or events.
- If the video does not show enough to answer, say so plainly and set insufficient_evidence=true. A short honest answer beats a guess.
- Do not identify people by name and do not guess identity, ethnicity, religion or other sensitive traits. Describe people by clothing, position and actions.
- Give times as seconds from the start of the video. Include timestamps for every moment you mention.
- Write `answer` in {language}, natural and brief (1-3 sentences). It will be read aloud by text-to-speech, so use no markdown, no lists and no emoji. Say times as they would be spoken.
- This is a conversation. Resolve references like "umwe muri bo" / "one of them", "iyo modoka" / "that car" or "hanyuma?" / "and then?" using the earlier turns and context notes.
- event_type must be one of: {event_types}. Use "observation" if none fit.
- Calibrate confidence honestly: low when objects are small, blurry, occluded or dark."""


def _source_context_text(query: AnalysisQuery) -> str:
    src = query.source
    lines = [f"Video source: {src.name}"]
    if src.location:
        lines.append(f"Camera location: {src.location}")
    lines.append("Type: live camera clip" if src.is_live else "Type: recorded video")
    if src.duration_seconds:
        lines.append(f"Duration: {src.duration_seconds:.0f} seconds")
    if query.window is not None and (query.window.start is not None or query.window.end is not None):
        lines.append(f"You are seeing only part of the video: from {query.window.start or 0:.0f}s to {query.window.end if query.window.end is not None else 'the end'}{'s' if query.window.end is not None else ''}. Give timestamps relative to the START OF THE FULL VIDEO.")
    if query.context_notes:
        lines.append("Conversation context so far:\n" + "\n".join(f"- {note}" for note in query.context_notes))
    return "\n".join(lines)


def _history_text(turn_content: str, timestamps: list[float]) -> str:
    if not timestamps:
        return turn_content
    return f"{turn_content}\n[referenced times: {', '.join(f'{t:.0f}s' for t in timestamps)}]"


class GeminiVideoAnalyzer(VideoAnalyzer):
    name = "gemini"
    capabilities = AnalyzerCapabilities(accepts_remote_uri=True)

    def __init__(self, *, api_key: str, model: str, timeout_seconds: int = 180, video_fps: float | None = None) -> None:
        # Imported lazily so the rest of the app never needs the SDK.
        from google import genai

        self.model = model
        self._timeout = timeout_seconds
        self._fps = video_fps
        self._client = genai.Client(api_key=api_key)

    # -- prepare -------------------------------------------------------------

    async def prepare(self, media: MediaHandle) -> PreparedVideo:
        from google.genai import errors, types

        if media.remote_uri:
            return PreparedVideo(analyzer=self.name, ref={"file_uri": media.remote_uri, "mime_type": None, "remote": True}, media_metadata=dict(media.metadata))
        if media.local_path is None:
            raise ProviderUnavailable("No media to send to Gemini")
        try:
            upload_config = types.UploadFileConfig(mime_type=media.mime_type)
            uploaded = await _with_retries(lambda: self._client.aio.files.upload(file=str(media.local_path), config=upload_config))
            deadline = time.monotonic() + self._timeout
            while _state(uploaded) == "PROCESSING":
                if time.monotonic() > deadline:
                    raise ProviderUnavailable("Gemini took too long to process the video", code="provider_timeout")
                await asyncio.sleep(2)
                uploaded = await self._client.aio.files.get(name=uploaded.name)
        except errors.APIError as exc:
            raise _provider_error(exc) from exc
        if _state(uploaded) != "ACTIVE":
            raise ProviderUnavailable("Gemini could not process this video", code="provider_rejected_video")
        expires = uploaded.expiration_time or (datetime.now(UTC) + _FILE_TTL)
        return PreparedVideo(
            analyzer=self.name,
            ref={"file_uri": uploaded.uri, "file_name": uploaded.name, "mime_type": uploaded.mime_type or media.mime_type},
            expires_at=min(expires, datetime.now(UTC) + _FILE_TTL),
            media_metadata=dict(media.metadata),
        )

    async def discard(self, prepared: PreparedVideo) -> None:
        from google.genai import errors

        name = prepared.ref.get("file_name")
        if not name:
            return
        try:
            await self._client.aio.files.delete(name=name)
        except errors.APIError as exc:
            logger.info("Could not delete Gemini file: %s", exc.code)

    # -- analyze -------------------------------------------------------------

    def build_contents(self, prepared: PreparedVideo, query: AnalysisQuery) -> list[Any]:
        from google.genai import types

        window = query.window
        offsets: dict[str, Any] = {}
        if window is not None and window.start is not None:
            offsets["start_offset"] = f"{max(0.0, window.start):.1f}s"
        if window is not None and window.end is not None:
            offsets["end_offset"] = f"{window.end:.1f}s"
        if self._fps:
            offsets["fps"] = self._fps
        video_part = types.Part(
            file_data=types.FileData(file_uri=prepared.ref["file_uri"], mime_type=prepared.ref.get("mime_type")),
            video_metadata=types.VideoMetadata(**offsets) if offsets else None,
        )
        contents: list[types.Content] = [
            types.Content(role="user", parts=[video_part, types.Part(text=_source_context_text(query))]),
            types.Content(role="model", parts=[types.Part(text="Understood. I will answer only from this video.")]),
        ]
        for turn in query.history:
            role = "user" if turn.role == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=_history_text(turn.content, turn.timestamps))]))
        contents.append(types.Content(role="user", parts=[types.Part(text=query.question)]))
        return contents

    async def analyze(self, prepared: PreparedVideo, query: AnalysisQuery) -> AnalysisResult:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=_system_instruction(query),
            response_mime_type="application/json",
            response_schema=_GeminiAnswer,
            temperature=0.2,
        )
        contents = self.build_contents(prepared, query)
        try:
            response = await asyncio.wait_for(
                _with_retries(lambda: self._client.aio.models.generate_content(model=self.model, contents=contents, config=config)),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise ProviderUnavailable("Gemini did not answer in time", code="provider_timeout") from exc
        except errors.APIError as exc:
            raise _provider_error(exc) from exc

        usage = Usage(
            input_tokens=getattr(response.usage_metadata, "prompt_token_count", None),
            output_tokens=getattr(response.usage_metadata, "candidates_token_count", None),
        )
        return normalize_response(response.text or "", model=self.model, language=query.language, usage=usage)


def normalize_response(text: str, *, model: str | None, language: str, usage: Usage | None = None) -> AnalysisResult:
    """Translate Gemini's JSON into the provider-neutral AnalysisResult."""
    try:
        parsed = _GeminiAnswer.model_validate_json(text)
    except ValidationError as exc:
        logger.warning("Gemini returned an unexpected response shape (%d errors)", exc.error_count())
        raise ProviderUnavailable("The video analyzer returned an unreadable answer", code="provider_bad_response") from exc

    def clamp(value: float | None) -> float | None:
        return None if value is None else max(0.0, min(1.0, value))

    return AnalysisResult(
        answer=parsed.answer.strip(),
        confidence=clamp(parsed.confidence) or 0.0,
        language=language,
        insufficient_evidence=parsed.insufficient_evidence,
        timestamps=sorted(
            (TimeReference(start_seconds=max(0.0, t.start_seconds), end_seconds=t.end_seconds, label=t.label) for t in parsed.timestamps),
            key=lambda t: t.start_seconds,
        ),
        events=[
            DetectedEvent(
                event_type=normalize_event_type(e.event_type),
                description=e.description,
                start_seconds=e.start_seconds,
                end_seconds=e.end_seconds,
                confidence=clamp(e.confidence),
                metadata={"provider_label": e.event_type} if normalize_event_type(e.event_type).value != e.event_type else {},
            )
            for e in parsed.events
        ],
        evidence=[Evidence(description=e.description, timestamp_seconds=e.timestamp_seconds) for e in parsed.evidence],
        referenced_entities=parsed.referenced_entities[:10],
        analyzer="gemini",
        model=model,
        usage=usage or Usage(),
        raw=parsed.model_dump(),
    )


async def _with_retries(call: Any) -> Any:
    """Retry transient Gemini errors (overloaded/unavailable) with backoff."""
    from google.genai import errors

    for delay in (*_RETRY_DELAYS, None):
        try:
            return await call()
        except errors.APIError as exc:
            if delay is None or getattr(exc, "code", None) not in _TRANSIENT_CODES:
                raise
            logger.info("Gemini transient error %s, retrying in %.1fs", exc.code, delay)
            await asyncio.sleep(delay)


def _state(file: Any) -> str:
    state = getattr(file, "state", None)
    return str(getattr(state, "name", state) or "").upper().removeprefix("FILESTATE.")


def _provider_error(exc: Any) -> ProviderUnavailable:
    code = getattr(exc, "code", None)
    logger.warning("Gemini API error: code=%s status=%s", code, getattr(exc, "status", None))
    if code in (401, 403):
        return ProviderUnavailable("Gemini rejected the API credentials. Check GEMINI_API_KEY.", code="provider_auth")
    if code == 429:
        if "PerDay" in str(getattr(exc, "details", "")):
            return ProviderUnavailable(
                "The daily Gemini quota for this API key is used up. It resets at midnight Pacific time, or enable billing for higher limits.",
                code="provider_daily_quota",
            )
        return ProviderUnavailable("Gemini quota exceeded. Try again in a minute.", code="provider_quota")
    if code in (503, 529):
        return ProviderUnavailable("Gemini is overloaded right now (high demand). Please try again in a minute.", code="provider_overloaded")
    if code == 404:
        return ProviderUnavailable("The configured Gemini model is not available. Check GEMINI_MODEL.", code="provider_model_unavailable")
    if code == 400:
        return ProviderUnavailable("Gemini could not process this request or video.", code="provider_bad_request")
    return ProviderUnavailable("The video analyzer is temporarily unavailable.", code="provider_error")
