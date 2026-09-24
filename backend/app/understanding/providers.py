"""VideoUnderstandingProvider implementations."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

import httpx

from app.analyzers.base import AnalysisQuery, TimeReference, Usage
from app.core.errors import AppError, ProviderUnavailable
from app.understanding.base import UnderstandingRequest, UnderstandingResult, VideoRef, VideoUnderstandingProvider
from app.video.sources.base import TimeWindow

logger = logging.getLogger(__name__)

GROUNDING_RULES = """Describe only what is visible. Do not invent objects or events.
Identify objects and animals by what they look like (e.g. "a large white rabbit", "a brown squirrel"), including things an object detector would not know.
Do not identify people by name or guess sensitive traits; describe clothing, position and actions.
If the frames do not show enough, say so. Be honest about uncertainty (small, blurry, dark, cartoon/animated content)."""


class GeminiVideoProvider(VideoUnderstandingProvider):
    """Whole-clip understanding with Gemini (uses the video already uploaded for the session)."""

    name = "gemini"

    def __init__(self, video_analyzer: Any) -> None:  # GeminiVideoAnalyzer
        self.video = video_analyzer
        self.model = getattr(video_analyzer, "model", None)

    async def understand(self, video: VideoRef, request: UnderstandingRequest) -> UnderstandingResult:
        if video.prepared is None or not video.prepared.ref.get("file_uri"):
            raise ProviderUnavailable("The video has not been prepared for Gemini yet", code="understanding_unavailable")
        window = TimeWindow(start=request.start_seconds, end=request.end_seconds) if (request.start_seconds is not None or request.end_seconds is not None) else None
        query = AnalysisQuery(question=request.question, language=request.language, source=request.source, window=window)
        result = await self.video.analyze(video.prepared.model_copy(update={"analyzer": "gemini"}), query)
        return UnderstandingResult(
            description=result.answer,
            observations=[e.description for e in result.evidence],
            timestamps=result.timestamps,
            events=[e.model_copy(update={"evidence_level": "model_interpretation"}) for e in result.events],
            confidence=result.confidence,
            insufficient_evidence=result.insufficient_evidence,
            provider=self.name,
            model=self.model,
            usage=result.usage,
        )


def sample_frames(path: Any, start: float | None, end: float | None, count: int, max_side: int = 768) -> list[tuple[float, bytes]]:
    """Evenly sample `count` JPEG frames between start and end (seconds)."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    duration = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / fps
    a = max(0.0, start or 0.0)
    b = min(duration, end if end is not None else duration)
    if b <= a:
        b = min(duration, a + 1.0)
    times = [a + (b - a) * (i + 0.5) / count for i in range(count)]
    frames: list[tuple[float, bytes]] = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_FRAMES, round(t * fps))
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        scale = max_side / max(h, w)
        if scale < 1:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        frames.append((round(t, 2), cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()))
    cap.release()
    return frames


def _parse_json(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {"description": text}


class OpenAICompatibleFramesProvider(VideoUnderstandingProvider):
    """Sends sampled frames to any OpenAI-compatible vision chat endpoint."""

    def __init__(self, *, name: str, base_url: str, model: str, api_key: str | None = None, frames: int = 6, timeout: float = 300, reasoning_effort: str | None = None) -> None:
        self.reasoning_effort = reasoning_effort  # reasoning models: None = model default (better grounded, slower)
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.frames = frames
        self.timeout = timeout

    async def understand(self, video: VideoRef, request: UnderstandingRequest) -> UnderstandingResult:
        if video.local_path is None or (not isinstance(video.local_path, str) and not video.local_path.exists()):
            raise ProviderUnavailable(f"{self.name} needs the stored video file", code="understanding_unavailable")
        frames = await asyncio.to_thread(sample_frames, video.local_path, request.start_seconds, request.end_seconds, self.frames)
        if not frames:
            raise ProviderUnavailable("Could not read frames from the video", code="understanding_unavailable")
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"{GROUNDING_RULES}\n\nThese are {len(frames)} frames from a video (camera '{request.source.name}'), taken at "
                f"{', '.join(f'{t:.1f}s' for t, _ in frames)} from the start of the video.\n\nQuestion: {request.question}\n\n"
                'Reply with JSON only: {"description": "...", "observations": ["..."], "objects": [{"name": "...", "timestamp_seconds": 0.0}], '
                '"confidence": 0.0, "insufficient_evidence": false}'
            ),
        }]
        for t, jpg in frames:
            content.append({"type": "text", "text": f"Frame at {t:.1f}s:"})
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}})
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        # Generous limit: reasoning models spend tokens thinking before the JSON answer.
        payload: dict[str, Any] = {"model": self.model, "messages": [{"role": "user", "content": content}], "temperature": 0.2, "max_tokens": 6000}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # Reasoning models occasionally finish without writing an answer: retry once,
        # then ask for a direct (non-reasoning) answer rather than return nothing.
        attempts = [payload, payload, {**payload, "reasoning_effort": "none"}]
        body: dict[str, Any] = {}
        data: dict[str, Any] = {}
        text = ""
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            for attempt in attempts:
                try:
                    response = await client.post(f"{self.base_url}/chat/completions", json=attempt, headers=headers)
                except httpx.HTTPError as exc:
                    raise ProviderUnavailable(f"{self.name} is not reachable at {self.base_url}", code="understanding_unavailable") from exc
                if response.status_code != 200:
                    raise ProviderUnavailable(f"{self.name} returned HTTP {response.status_code}: {response.text[:200]}", code="understanding_error")
                body = response.json()
                text = body["choices"][0]["message"].get("content") or ""
                data = _parse_json(text)
                if str(data.get("description") or "").strip():
                    break
                logger.info("%s returned an empty answer; retrying", self.name)
        if not str(data.get("description") or "").strip():
            raise ProviderUnavailable(f"{self.name} returned no answer", code="understanding_error")
        usage = body.get("usage") or {}
        objects = [o for o in data.get("objects") or [] if isinstance(o, dict) and o.get("name")]
        confidence = data.get("confidence")
        return UnderstandingResult(
            description=str(data.get("description") or text).strip(),
            observations=[str(o) for o in data.get("observations") or []] + [f"{o['name']} at {o.get('timestamp_seconds', '?')}s" for o in objects],
            timestamps=[TimeReference(start_seconds=float(o["timestamp_seconds"]), label=str(o["name"])) for o in objects if isinstance(o.get("timestamp_seconds"), (int, float))],
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            insufficient_evidence=bool(data.get("insufficient_evidence")),
            provider=self.name,
            model=self.model,
            usage=Usage(input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens")),
        )


class LocalVLMProvider(OpenAICompatibleFramesProvider):
    """A vision-language model on this machine/network (Ollama, LM Studio, vLLM)."""

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None, frames: int = 6, reasoning_effort: str | None = None) -> None:
        super().__init__(name="local_vlm", base_url=base_url, model=model, api_key=api_key, frames=frames, reasoning_effort=reasoning_effort)


class TogetherVideoProvider(OpenAICompatibleFramesProvider):
    """Together AI hosted vision models (OpenAI-compatible API)."""

    def __init__(self, *, api_key: str, model: str, frames: int = 6) -> None:
        super().__init__(name="together", base_url="https://api.together.xyz/v1", model=model, api_key=api_key, frames=frames)


class FallbackProvider(VideoUnderstandingProvider):
    """Try providers in order; the next one is used when one is unavailable (e.g. quota)."""

    def __init__(self, providers: list[VideoUnderstandingProvider]) -> None:
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.model = providers[0].model

    async def understand(self, video: VideoRef, request: UnderstandingRequest) -> UnderstandingResult:
        errors: list[str] = []
        for provider in self.providers:
            try:
                return await provider.understand(video, request)
            except AppError as exc:
                logger.info("Understanding provider %s unavailable (%s); trying next", provider.name, exc.code)
                errors.append(f"{provider.name}: {exc.message}")
        raise ProviderUnavailable("No video understanding provider could answer. " + " | ".join(errors), code="understanding_unavailable")
