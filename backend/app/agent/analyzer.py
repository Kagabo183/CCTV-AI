"""AgentVideoAnalyzer: the conversational agent over local vision memory.

    question -> LLM (Gemini, text only) --tool calls--> VisionMemory (local events/tracks)
                                        \\--escalation--> GeminiVideoAnalyzer on a clip
             -> final_answer (answer + timestamps + evidence with levels)

It implements the VideoAnalyzer interface, so the orchestrator, API and UI are
unchanged. Local tools cost no video tokens; the video is only sent to Gemini
when the agent decides local evidence cannot answer (analyze_video_clip).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.tools import TOOL_SCHEMAS, ToolError, VisionMemory
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
from app.analyzers.gemini import LANGUAGE_NAMES, GeminiVideoAnalyzer, _provider_error, _with_retries
from app.core.errors import AppError, ProviderUnavailable
from app.db.session import get_sessionmaker
from app.video.sources.base import MediaHandle, TimeWindow

logger = logging.getLogger(__name__)


def system_prompt(query: AnalysisQuery) -> str:
    language = LANGUAGE_NAMES.get(query.language, query.language)
    src = query.source
    return f"""You are the conversational assistant of a CCTV platform used in Rwanda. The user talks to their cameras, mostly in Kinyarwanda.
You cannot see video yourself. You answer only from tool results.

Selected camera: "{src.name}"{f' (location: {src.location})' if src.location else ''}, camera_id {src.source_id}. It is a RECORDED video{f' of {src.duration_seconds:.0f} s' if src.duration_seconds else ''}: "now" means the end of the recording and times are seconds from its start.

Kinds of evidence (never blur them):
- tracking: objects detected by a local detector (YOLO) and followed by a tracker. Labels are generic COCO classes (person, car, bicycle, ...).
- rule: events computed from tracks by geometric/time rules (appeared, left view, entered/exited a zone or tripwire, stayed, loitering = long time in view, crowd).
- model_interpretation: what a vision-language model (analyze_video_clip) says it sees. Attribute it as such.

How to work:
1. Counting, presence, arrivals and departures, how long someone stayed, what happened in a time range: use the local tools (get_current_objects, count_objects, get_recent_events, get_camera_status). They are free and precise about positions and times.
2. Appearance, clothing, colours, what someone carries or does, interactions, anything a class label cannot express, or local results that look uncertain: call analyze_video_clip. Use local events to pick a short time window when possible.
3. If local analysis is unavailable (processing, failed, or a YouTube link), use analyze_video_clip.
4. Clock times ("saa munani" = 14:00) only work if the camera has a known recording start. Otherwise say the video has no clock time.
5. Counting caveat: one person hidden and seen again can get a new track id. For "how many are there", prefer the number visible at the same time. Say "about" when unsure.

Honesty rules:
- NEVER claim intent, behaviour or wrongdoing (stealing, suspicious, fighting, breaking in) from detection or tracking. Only report it if analyze_video_clip explicitly observed it, and present it as what the video analysis saw.
- "loitering" here only means a long time in view. Describe it as that.
- If the evidence does not answer the question, say so.

Answer: always finish by calling final_answer. Write `answer` in {language}, natural, 1-3 short spoken sentences, no markdown, with times said naturally.
Include timestamps for the moments you mention and evidence items with their level."""


class AgentVideoAnalyzer(VideoAnalyzer):
    name = "agent"
    capabilities = AnalyzerCapabilities(accepts_remote_uri=True, continuous_events=True)

    def __init__(self, *, video_analyzer: GeminiVideoAnalyzer, model: str, max_steps: int = 6) -> None:
        self.video = video_analyzer
        self.model = model
        self.max_steps = max_steps
        self._client = video_analyzer._client

    async def prepare(self, media: MediaHandle) -> PreparedVideo:
        # Uploading to the Files API is storage, not inference: it makes escalation
        # fast when needed. No frames are analysed unless the agent escalates.
        prepared = await self.video.prepare(media)
        return prepared.model_copy(update={"analyzer": self.name})

    async def discard(self, prepared: PreparedVideo) -> None:
        await self.video.discard(prepared)

    async def analyze(self, prepared: PreparedVideo, query: AnalysisQuery) -> AnalysisResult:
        from google.genai import errors, types

        if query.user_id is None:
            raise ProviderUnavailable("Agent needs the user context", code="agent_misconfigured")

        tools = [types.Tool(function_declarations=[types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in TOOL_SCHEMAS])]
        contents: list[types.Content] = []
        for turn in query.history:
            contents.append(types.Content(role="user" if turn.role == "user" else "model", parts=[types.Part(text=turn.content)]))
        notes = ("\nConversation context: " + " | ".join(query.context_notes)) if query.context_notes else ""
        contents.append(types.Content(role="user", parts=[types.Part(text=f"{query.question}{notes}")]))

        usage = Usage(input_tokens=0, output_tokens=0)
        trace: dict[str, Any] = {"tools_used": [], "escalated": False}
        escalation_events: list[DetectedEvent] = []

        async with get_sessionmaker()() as db:
            memory = VisionMemory(db, query.user_id, query.source.source_id)
            for step in range(self.max_steps):
                last = step == self.max_steps - 1
                config = types.GenerateContentConfig(
                    system_instruction=system_prompt(query),
                    tools=tools,
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=["final_answer"] if last else None)
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    temperature=0.2,
                )
                try:
                    response = await _with_retries(lambda: self._client.aio.models.generate_content(model=self.model, contents=contents, config=config))
                except errors.APIError as exc:
                    raise _provider_error(exc) from exc
                meta = response.usage_metadata
                usage.input_tokens += getattr(meta, "prompt_token_count", 0) or 0
                usage.output_tokens += getattr(meta, "candidates_token_count", 0) or 0

                calls = response.function_calls or []
                if not calls:
                    raise ProviderUnavailable("The assistant did not produce an answer", code="agent_no_answer")
                contents.append(response.candidates[0].content)  # keeps thought signatures for the next turn

                responses = []
                for call in calls:
                    args = dict(call.args or {})
                    if call.name == "final_answer":
                        trace["steps"] = step + 1
                        return self._result(args, query, usage, trace, escalation_events)
                    trace["tools_used"].append(call.name)
                    result = await self._dispatch(call.name, args, memory, prepared, query, usage, trace, escalation_events)
                    responses.append(types.Part.from_function_response(name=call.name, response={"result": result}))
                # The Gemini API accepts only "user"/"model" roles: function results go in a user turn.
                contents.append(types.Content(role="user", parts=responses))
        raise ProviderUnavailable("The assistant could not finish the answer", code="agent_no_answer")

    async def _dispatch(
        self,
        name: str,
        args: dict[str, Any],
        memory: VisionMemory,
        prepared: PreparedVideo,
        query: AnalysisQuery,
        usage: Usage,
        trace: dict[str, Any],
        escalation_events: list[DetectedEvent],
    ) -> Any:
        try:
            if name == "analyze_video_clip":
                return await self._escalate(args, prepared, query, usage, trace, escalation_events)
            method = getattr(memory, name, None)
            if method is None or name.startswith("_"):
                return {"error": f"Unknown tool {name}"}
            return _jsonable(await method(**args))
        except ToolError as exc:
            return {"error": str(exc)}
        except TypeError as exc:  # model passed unexpected arguments
            return {"error": f"Bad arguments for {name}: {exc}"}

    async def _escalate(self, args: dict[str, Any], prepared: PreparedVideo, query: AnalysisQuery, usage: Usage, trace: dict[str, Any], events: list[DetectedEvent]) -> dict[str, Any]:
        """Deep video understanding on the (optionally windowed) clip."""
        if args.get("camera_id") and str(args["camera_id"]) not in (str(query.source.source_id), query.source.location, query.source.name):
            return {"error": "analyze_video_clip only works on the selected camera in this conversation."}
        start, end = args.get("start_seconds"), args.get("end_seconds")
        window = TimeWindow(start=float(start) if start is not None else None, end=float(end) if end is not None else None)
        sub_query = AnalysisQuery(
            question=str(args.get("question") or query.question),
            language="en",  # the agent reads it; the final answer is written in the user's language
            source=query.source,
            window=window if (window.start is not None or window.end is not None) else None,
        )
        try:
            result = await self.video.analyze(prepared.model_copy(update={"analyzer": "gemini"}), sub_query)
        except AppError as exc:
            return {"error": f"Video analysis failed: {exc.message}"}
        trace["escalated"] = True
        usage.input_tokens = (usage.input_tokens or 0) + (result.usage.input_tokens or 0)
        usage.output_tokens = (usage.output_tokens or 0) + (result.usage.output_tokens or 0)
        events.extend(e.model_copy(update={"evidence_level": "model_interpretation"}) for e in result.events)
        return {
            "evidence_level": "model_interpretation",
            "observations": result.answer,
            "confidence": result.confidence,
            "insufficient_evidence": result.insufficient_evidence,
            "timestamps": [t.model_dump() for t in result.timestamps],
            "evidence": [e.description for e in result.evidence],
        }

    def _result(self, args: dict[str, Any], query: AnalysisQuery, usage: Usage, trace: dict[str, Any], events: list[DetectedEvent]) -> AnalysisResult:
        levels = {e.get("level") for e in args.get("evidence") or [] if isinstance(e, dict)}
        trace["evidence_levels"] = sorted(level for level in levels if level)
        return AnalysisResult(
            answer=str(args.get("answer", "")).strip(),
            confidence=max(0.0, min(1.0, float(args.get("confidence") or 0.0))),
            language=query.language,
            insufficient_evidence=bool(args.get("insufficient_evidence")),
            timestamps=sorted(
                (TimeReference(start_seconds=max(0.0, float(t["start_seconds"])), end_seconds=t.get("end_seconds"), label=str(t.get("label", ""))) for t in args.get("timestamps") or [] if "start_seconds" in t),
                key=lambda t: t.start_seconds,
            ),
            evidence=[Evidence(description=str(e.get("description", "")), timestamp_seconds=e.get("timestamp_seconds"), level=e.get("level")) for e in args.get("evidence") or [] if isinstance(e, dict)],
            events=events,
            analyzer=self.name,
            model=self.model,
            usage=usage,
            trace=trace,
        )


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
