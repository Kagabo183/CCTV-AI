"""AgentVideoAnalyzer: the conversational agent over local vision memory.

    question -> LLM (Gemini, text only) --tool calls--> VisionMemory (local events/tracks)
                                        \\--escalation--> GeminiVideoAnalyzer on a clip
             -> final_answer (answer + timestamps + evidence with levels)

Escalation goes through a VideoUnderstandingProvider (Gemini, a local VLM, or
Together AI) so open-ended "what is that?" questions are answered by a model
that is not limited to the detector's class list.

It implements the VideoAnalyzer interface, so the orchestrator, API and UI are
unchanged. Local tools cost no video tokens; the video is only sent to Gemini
when the agent decides local evidence cannot answer (analyze_video_clip).
"""

from __future__ import annotations

import re

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
from app.agent.llm import AgentChat, GeminiAgentChat, OpenAIAgentChat
from app.analyzers.gemini import LANGUAGE_NAMES, GeminiVideoAnalyzer
from app.core.errors import AppError, ProviderUnavailable
from app.db.session import get_sessionmaker
from app.understanding.base import UnderstandingRequest, VideoRef, VideoUnderstandingProvider
from app.understanding.factory import get_understanding_provider
from app.understanding.providers import GeminiVideoProvider
from app.video.sources.base import MediaHandle

logger = logging.getLogger(__name__)


_WILDLIFE_OVERVIEW = re.compile(
    r"^(how many|which|what)( kinds? of| types? of)? animals?( are| were| is)?( there| seen| visible)?( (in|on) (the|this) (video|camera|recording))?\s*\??\s*(\(answer in english\.\))?$",
    re.IGNORECASE,
)
# Live cameras: "what is happening (now)?" and "what happened in the last N minutes?" are answered straight
# from the live AI (current picture, events, peak counts): exact, instant, and no small-model tool guessing.
_LIVE_NOW = re.compile(
    r"\b(what'?s|what is|what are)\b.*\b(happening|going on|there|visible|in view|you see)\b|\b(what'?s|what is) (on|at|in front of)\b|\bwho is (there|at)\b|\bis (there )?any(one|body)\b",
    re.IGNORECASE,
)
_LIVE_PAST = re.compile(r"\b(last|past|previous)\s+(?:(\d+|a|an|one|two|three|five|ten|fifteen|twenty|thirty|forty|fifty|sixty)\s*)?(min(ute)?s?|h(ou)?rs?)\b", re.IGNORECASE)
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
_LIVE_ANIMALS = re.compile(r"\b(any|which|what|are there)\b.*\banimals?\b|\banimals?\b.*\b(there|now|visible|on (the )?camera)\b", re.IGNORECASE)
_LIVE_ARRIVAL = re.compile(r"\bwhen did (?:the |a |an |that )?([a-z][a-z ]*?)s? (?:arrive|come|appear|show up|get there|enter|first appear)", re.IGNORECASE)
_LIVE_COUNT = re.compile(r"\bhow many ([a-z]+)", re.IGNORECASE)
_CLASS_WORDS = {"people": ["person"], "persons": ["person"], "person": ["person"], "men": ["person"], "women": ["person"],
                "cars": ["car"], "car": ["car"], "vehicles": ["car", "truck", "bus", "motorcycle"], "trucks": ["truck"], "buses": ["bus"],
                "motorcycles": ["motorcycle"], "motorbikes": ["motorcycle"], "bicycles": ["bicycle"], "bikes": ["bicycle"]}
_OPEN_QUESTION = re.compile(r"\b(why|wear|wearing|colou?r|carry|carrying|carries|doing|hold|holds|holding|describe|look like)\b", re.IGNORECASE)

_FACTUAL_ANIMAL_QUESTION = re.compile(r"\b(how many|which|what animals|what kinds?|where|when|arrive|arrived|count|number of|species)\b", re.IGNORECASE)


def _time_model(src: Any) -> str:
    if src.is_live:
        return ("It is a LIVE camera: \"now\" means right now. Use get_current_objects for what is happening now, and last_seconds "
                "(\"the last 20 minutes\" = 1200) or start_clock/end_clock (Kigali time) for the past; never video seconds. "
                "Say times as clock times. Other cameras can be asked about by passing their name as camera_id (see list_cameras).")
    return (f"It is a RECORDED video{f' of {src.duration_seconds:.0f} s' if src.duration_seconds else ''}: "
            "\"now\" means the end of the recording and times are seconds from its start.")


def system_prompt(query: AnalysisQuery) -> str:
    language = LANGUAGE_NAMES.get(query.language, query.language)
    src = query.source
    return f"""You are the conversational assistant of a CCTV platform used in Rwanda. The user talks to their cameras, mostly in Kinyarwanda.
You cannot see video yourself. You answer only from tool results.

Selected camera: "{src.name}"{f' (location: {src.location})' if src.location else ''}, camera_id {src.source_id}. {_time_model(src)}

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
6. The detector is CLOSED-SET: it only knows a fixed list of classes (COCO: 80, Objects365: 365) and confidently mislabels things outside it (a cartoon rabbit came out as "person"). Tracks the detector was not sure about are labelled "unknown" with a candidate class and a confidence (see list_uncertain_objects).
   - Never state an unknown track's candidate as fact: say "an unidentified object, possibly a person (confidence 0.41)".
   - For "what is that object/animal?", "what is it doing?", "what changed?", "describe everything", unknown objects, or anything a class label cannot express: call analyze_video_clip on that time window.

Numbers: when a tool result has a "summary", use its numbers exactly. Never invent or estimate a number that no tool returned.

Animals: for which animals, how many, where, or when an animal arrived/left, use list_wildlife (pass species and a time range when asked).
Do not call analyze_video_clip for those. Only use analyze_video_clip for behaviour or reasons ("what is the hippo doing?",
"why are they moving away?"). If a species is "possible" or "uncertain", say that it is not certain.

Honesty rules:
- NEVER claim intent, behaviour or wrongdoing (stealing, suspicious, fighting, breaking in) from detection or tracking. Only report it if analyze_video_clip explicitly observed it, and present it as what the video analysis saw.
- "loitering" here only means a long time in view. Describe it as that.
- If the evidence does not answer the question, say so.

Answer: always finish by calling final_answer. Write `answer` in {language}, natural, 1-3 short spoken sentences, no markdown, with times said naturally.
Include timestamps for the moments you mention and evidence items with their level."""


class AgentVideoAnalyzer(VideoAnalyzer):
    name = "agent"
    capabilities = AnalyzerCapabilities(accepts_remote_uri=True, continuous_events=True)

    def __init__(
        self,
        *,
        video_analyzer: GeminiVideoAnalyzer | None,
        model: str,
        max_steps: int = 6,
        understanding: VideoUnderstandingProvider | None = None,
        local_llm: dict[str, Any] | None = None,
    ) -> None:
        """video_analyzer=None + local_llm={base_url, model, api_key, reasoning_effort} runs fully
        locally: no Gemini upload, a local model drives the tools, a local VLM answers visual questions."""
        self.video = video_analyzer
        self.model = model
        self.max_steps = max_steps
        self._client = video_analyzer._client if video_analyzer is not None else None
        self._understanding = understanding
        self.local_llm = local_llm

    def _chat(self, system: str, query: AnalysisQuery, user_text: str) -> AgentChat:
        if self.local_llm:
            return OpenAIAgentChat(system=system, history=query.history, user_text=user_text, tool_schemas=TOOL_SCHEMAS, **self.local_llm)
        return GeminiAgentChat(self._client, self.model, system, query.history, user_text, TOOL_SCHEMAS)

    @property
    def understanding(self) -> VideoUnderstandingProvider:
        """Configured VLM provider(s); Gemini on the session video if none is configured."""
        if self._understanding is None:
            try:
                self._understanding = get_understanding_provider()
            except AppError:
                if self.video is None:
                    raise
                self._understanding = GeminiVideoProvider(self.video)
        return self._understanding

    async def prepare(self, media: MediaHandle) -> PreparedVideo:
        if self.video is None:  # fully local: frame-based VLMs read the stored file directly
            return PreparedVideo(analyzer=self.name, ref={"local": True}, media_metadata=dict(media.metadata))
        # Uploading to the Files API is storage, not inference: it makes escalation
        # fast when needed. No frames are analysed unless the agent escalates.
        prepared = await self.video.prepare(media)
        return prepared.model_copy(update={"analyzer": self.name})

    async def discard(self, prepared: PreparedVideo) -> None:
        if self.video is not None:
            await self.video.discard(prepared)

    async def analyze(self, prepared: PreparedVideo, query: AnalysisQuery) -> AnalysisResult:
        if query.user_id is None:
            raise ProviderUnavailable("Agent needs the user context", code="agent_misconfigured")

        # Local models work in English; NLLB translates question/history in and the answer out.
        translator = self._translator() if (self.local_llm and query.language != "en") else None
        user_language = query.language
        if translator is not None:
            query = await self._to_english(query, translator)

        live = await self._direct_live_answer(query)
        if live is not None:
            direct, route = live
            trace = {"tools_used": direct.pop("_tools"), "escalated": False, "agent_llm": "rule", "agent_model": None, "route": route}
            return await self._finish(direct, query, Usage(input_tokens=0, output_tokens=0), trace, [], translator, user_language)

        direct = await self._direct_wildlife_answer(query)
        if direct is not None:  # "how many / which animals": answered from the wildlife data, no LLM guesswork
            trace = {"tools_used": ["list_wildlife"], "escalated": False, "agent_llm": "rule", "agent_model": None, "route": "wildlife_overview"}
            return await self._finish(direct, query, Usage(input_tokens=0, output_tokens=0), trace, [], translator, user_language)

        notes = ("\nConversation context: " + " | ".join(query.context_notes)) if query.context_notes else ""
        facts = await self._facts(query)
        chat = self._chat(system_prompt(query), query, f"{query.question}{notes}{facts}")
        trace: dict[str, Any] = {"tools_used": [], "escalated": False, "agent_llm": "local" if self.local_llm else "gemini", "agent_model": self.model}
        escalation_events: list[DetectedEvent] = []
        extra_usage = Usage(input_tokens=0, output_tokens=0)  # escalation tokens

        def usage() -> Usage:
            return Usage(input_tokens=(chat.usage.input_tokens or 0) + (extra_usage.input_tokens or 0), output_tokens=(chat.usage.output_tokens or 0) + (extra_usage.output_tokens or 0))

        nudged = False
        async with get_sessionmaker()() as db:
            memory = VisionMemory(db, query.user_id, query.source.source_id)
            for step in range(self.max_steps):
                result = await chat.step(force_final=step == self.max_steps - 1)
                if not result.calls:
                    if result.text and nudged:  # some local models answer in plain text: accept it, marked as such
                        trace["steps"], trace["unstructured_answer"] = step + 1, True
                        return await self._finish({"answer": result.text, "confidence": 0.3, "insufficient_evidence": False}, query, usage(), trace, escalation_events, translator, user_language)
                    if nudged:
                        break
                    chat.nudge("Use the tools, then call final_answer with your answer.")
                    nudged = True
                    continue
                results = []
                for call in result.calls:
                    if call.name == "final_answer":
                        trace["steps"] = step + 1
                        return await self._finish(call.args, query, usage(), trace, escalation_events, translator, user_language)
                    trace["tools_used"].append(call.name)
                    results.append((call, await self._dispatch(call.name, call.args, memory, prepared, query, extra_usage, trace, escalation_events)))
                chat.add_results(results)
        raise ProviderUnavailable("The assistant could not finish the answer", code="agent_no_answer")

    @staticmethod
    async def _direct_live_answer(query: AnalysisQuery) -> tuple[dict[str, Any], str] | None:
        """'What is happening on Camera 3?' / 'What happened in the last 20 minutes?' on a live camera."""
        if not query.source.is_live:
            return None
        question = query.question.replace("(Answer in English.)", "").strip()
        if _OPEN_QUESTION.search(question):
            return None  # appearance / behaviour: the agent decides whether to look at the video
        past = _LIVE_PAST.search(question)
        arrival, count = _LIVE_ARRIVAL.search(question), _LIVE_COUNT.search(question)
        animals = _LIVE_ANIMALS.search(question) or (count and count.group(1).lower().startswith("animal"))
        if past is None and not (_LIVE_NOW.search(question) or animals or arrival or (count and count.group(1).lower() in _CLASS_WORDS)):
            return None
        async with get_sessionmaker()() as db:
            memory = VisionMemory(db, query.user_id, query.source.source_id)  # type: ignore[arg-type]
            try:
                if arrival is not None:
                    return await AgentVideoAnalyzer._live_arrival(memory, arrival.group(1).strip().lower())
                if animals:
                    result = await memory.list_wildlife()
                    return ({"answer": result["summary"], "confidence": 0.8, "insufficient_evidence": False, "_tools": ["list_wildlife"],
                             "evidence": [{"description": result["summary"], "level": "detection"}], "timestamps": []}, "live_animals")
                if count is not None and past is None:
                    classes = _CLASS_WORDS[count.group(1).lower()]
                    now = await memory.get_current_objects()
                    n = sum(int((now.get("counts") or {}).get(c, 0)) for c in classes)
                    recent = await memory.count_objects(last_seconds=600)
                    peak = max((int(recent["max_simultaneously_visible_by_class"].get(c, 0)) for c in classes), default=0)
                    word = count.group(1).lower()
                    # phrased for machine translation: "the highest number at the same time" came out as "the number of dead"
                    answer = f"Right now no {word} are in view." if n == 0 else f"Right now there {'is' if n == 1 else 'are'} {n} {word if n != 1 else classes[0]} in view."
                    if peak > n:
                        answer += f" In the last 10 minutes, the camera saw up to {peak} {word} at once."
                    return ({"answer": answer, "confidence": 0.8, "insufficient_evidence": False, "_tools": ["get_current_objects", "count_objects"],
                             "evidence": [{"description": now["summary"], "level": "tracking"}, {"description": recent["summary"], "level": "tracking"}], "timestamps": []}, "live_count")
                if past is None:
                    now = await memory.get_current_objects()
                    recent = await memory.get_recent_events(last_seconds=600)
                    moves = [e for e in recent["events"] if e["type"] not in ("object_appeared", "object_disappeared")]
                    answer = now["summary"]
                    n = len(recent["events"])
                    if n:
                        answer += f" In the last 10 minutes there {'was 1 detection event' if n == 1 else f'were {n} detection events'}" + (f", including {moves[-1]['description']} at {moves[-1]['time_kigali']}" if moves else "") + "."
                    return ({"answer": answer, "confidence": 0.8, "insufficient_evidence": False, "_tools": ["get_current_objects", "get_recent_events"],
                             "evidence": [{"description": now["summary"], "level": "tracking"}, {"description": recent["summary"], "level": "rule"}], "timestamps": []}, "live_now")
                amount = (past.group(2) or "one").lower()
                value = int(amount) if amount.isdigit() else _NUMBER_WORDS[amount]
                seconds = value * (3600 if past.group(3).lower().startswith("h") else 60)
                counts = await memory.count_objects(last_seconds=seconds)
                events = await memory.get_recent_events(last_seconds=seconds)
            except ToolError:
                return None
        answer = f"{counts['summary']} {events['summary']}"
        return ({"answer": answer, "confidence": 0.8, "insufficient_evidence": False, "_tools": ["count_objects", "get_recent_events"],
                 "evidence": [{"description": counts["summary"], "level": "tracking"}, {"description": events["summary"], "level": "rule"}], "timestamps": []}, "live_period")

    @staticmethod
    async def _live_arrival(memory: VisionMemory, what: str) -> tuple[dict[str, Any], str]:
        """'When did the elephant arrive?': the first sighting of that species (or object class) in the last hour."""
        what = {"hippo": "hippopotamus", "rhino": "rhinoceros", "people": "person"}.get(what, what)
        result = await memory.list_wildlife(species=what)
        groups = result.get("species") or []
        if groups:
            g = min(groups, key=lambda x: x["first_seen_kigali"])
            still = what in " ".join(result.get("visible_now", {})).lower()
            answer = f"The first {g['label']} was seen at {g['first_seen_kigali']}" + (" and it is still in view." if still else f"; it was last seen at {g['last_seen_kigali']}.")
            if not g["certain"]:
                answer += " The species is not certain."
            return ({"answer": answer, "confidence": 0.75, "insufficient_evidence": False, "_tools": ["list_wildlife"],
                     "evidence": [{"description": result["summary"], "level": "detection"}], "timestamps": []}, "live_arrival")
        events = await memory.get_recent_events(event_types=["object_appeared", "person_entered", "vehicle_entered", "animal_entered"], last_seconds=3600)
        hits = [e for e in events["events"] if what in (e.get("class") or "").lower()]
        if hits:
            answer = f"A {hits[0]['class']} first appeared at {hits[0]['time_kigali']} in the last hour" + (f", and {len(hits) - 1} more times after that." if len(hits) > 1 else ".")
        else:
            answer = f"No {what} was seen on this camera in the last hour."
        return ({"answer": answer, "confidence": 0.7, "insufficient_evidence": not hits, "_tools": ["list_wildlife", "get_recent_events"],
                 "evidence": [{"description": events["summary"], "level": "rule"}], "timestamps": []}, "live_arrival")

    @staticmethod
    async def _direct_wildlife_answer(query: AnalysisQuery) -> dict[str, Any] | None:
        """Overview questions ("how many animals?", "which animals are there?") get a template answer built
        from list_wildlife: small local models tend to pick random filters for them."""
        if query.source.is_live or not _WILDLIFE_OVERVIEW.match(query.question.strip()):
            return None
        async with get_sessionmaker()() as db:
            try:
                result = await VisionMemory(db, query.user_id, query.source.source_id).list_wildlife()  # type: ignore[arg-type]
            except ToolError:
                return None
        if not result.get("available", True) or not result.get("species"):
            return None
        groups = result["species"]
        sure = sorted((g for g in groups if g["certain"]), key=lambda g: -g["animals"])
        unsure = [g for g in groups if not g["certain"]]
        parts = [f"{g['animals']} {g['label']}" for g in sure]
        answer = (f"The wildlife detector found {len(sure)} species it is confident about: " + ", ".join(parts) + ". " if sure else "")
        uncertain_n = sum(g["animals"] for g in unsure)
        if uncertain_n:
            answer += f"{uncertain_n} more animals could not be identified with confidence"
            guesses = [g["label"].replace("possible ", "") for g in unsure if g["label"].startswith("possible ")][:4]
            answer += f" (possibly {', '.join(guesses)})." if guesses else "."
        answer += " Counts are separate tracked animals; the same animal can be counted again after it leaves and returns."
        return {"answer": answer, "confidence": 0.85, "insufficient_evidence": False,
                "evidence": [{"description": result["summary"], "level": "detection"}],
                "timestamps": [{"start_seconds": g["first_seen"], "end_seconds": g["last_seen"], "label": g["label"]} for g in sure[:5]]}

    @staticmethod
    async def _facts(query: AnalysisQuery) -> str:
        """The detector's counts for this video, given up front: small models otherwise skip the
        counting tool and ask the video AI, which only sees a few frames of a long video."""
        facts = []
        async with get_sessionmaker()() as db:
            memory = VisionMemory(db, query.user_id, query.source.source_id)  # type: ignore[arg-type]
            for tool in (memory.count_objects, memory.list_wildlife):
                try:
                    result = await tool()
                except Exception:  # noqa: BLE001 - no local analysis yet: the agent works without it
                    continue
                if result.get("summary") and result.get("available", True):
                    facts.append(result["summary"])
            if len(facts) == 2:
                # species are known: drop the general detector's COCO animal guesses ("cow", "bear" ...)
                from app.vision.types import ANIMAL_CLASSES

                head, _, tail = facts[0].partition(": ")
                parts = [p for p in tail.split("; ") if p.split(":")[0].strip() not in ANIMAL_CLASSES]
                facts[0] = f"{head}: " + "; ".join(parts) if parts else ""
                facts = [f for f in facts if f]
        if query.source.is_live:
            return ("\nLive detector facts for the last hour (Kigali time; for 'now' call get_current_objects): " + " | ".join(facts)) if facts else ""
        return ("\nDetector facts for this video (use these numbers for counting and presence questions; for animals prefer the Wildlife line): "
                + " | ".join(facts)) if facts else ""

    @staticmethod
    def _translator():  # type: ignore[no-untyped-def]
        from app.language.translate import get_translator

        return get_translator()

    async def _to_english(self, query: AnalysisQuery, translator) -> AnalysisQuery:  # type: ignore[no-untyped-def]
        src = query.language
        question = await translator.translate(query.question, src, "en")
        history = [
            turn.model_copy(update={"content": await translator.translate(turn.content, src, "en")}) for turn in query.history
        ]
        # Only the English text: small models copy any other language they see into their answer.
        return query.model_copy(update={"question": f"{question}\n(Answer in English.)", "history": history, "language": "en"})

    async def _finish(self, args, query, usage, trace, events, translator, user_language):  # type: ignore[no-untyped-def]
        answer = str(args.get("answer") or "")
        m = re.search(r"final[ _]answer\s*:\s*(.+)$", answer, re.IGNORECASE | re.DOTALL)
        if m and m.group(1).strip():  # small models sometimes write their reasoning, then "Final answer: ..."
            args = {**args, "answer": m.group(1).strip()}
        result = self._result(args, query, usage, trace, events)
        if translator is not None and result.answer:
            trace["answer_en"] = result.answer
            trace["translated_by"] = translator.name
            result = result.model_copy(update={"answer": await translator.translate(result.answer, "en", user_language), "language": user_language, "trace": trace})
        return result

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
        logger.info("agent tool %s %s", name, json.dumps(args, default=str)[:200])
        try:
            if name == "analyze_video_clip":
                if "list_wildlife" in trace["tools_used"] and _FACTUAL_ANIMAL_QUESTION.search(query.question):
                    # Small local models escalate even when list_wildlife already answered; the video AI only sees
                    # a few frames of a long video and is slow, so counts / lists / where / when stay local.
                    return {"error": "Not needed: answer this from the list_wildlife result you already have (counts, species, where, when). "
                                     "Now call final_answer."}
                return await self._escalate(args, memory, prepared, query, usage, trace, escalation_events)
            method = getattr(memory, name, None)
            if method is None or name.startswith("_"):
                return {"error": f"Unknown tool {name}"}
            return _jsonable(await method(**args))
        except ToolError as exc:
            return {"error": str(exc)}
        except TypeError as exc:  # model passed unexpected arguments
            return {"error": f"Bad arguments for {name}: {exc}"}

    async def _escalate(self, args: dict[str, Any], memory: VisionMemory, prepared: PreparedVideo, query: AnalysisQuery, usage: Usage, trace: dict[str, Any], events: list[DetectedEvent]) -> dict[str, Any]:
        """Open-ended visual understanding of the (optionally windowed) clip by a vision-language model."""
        if args.get("camera_id") and str(args["camera_id"]) not in (str(query.source.source_id), query.source.location, query.source.name):
            # Small models often put a place or object name here. Only refuse when it clearly names another camera.
            try:
                other = await memory._source(str(args["camera_id"]))
            except ToolError:
                other = None
            if other is not None and other.id != query.source.source_id:
                return {"error": f"analyze_video_clip only works on the selected camera ({query.source.name}), not {other.name}."}
        start, end = args.get("start_seconds"), args.get("end_seconds")
        if query.source.is_live:
            start = end = None  # the model looks at a clip recorded now, not at a position in a video
        request = UnderstandingRequest(
            question=str(args.get("question") or query.question),
            language="en",  # the agent reads it; the final answer is written in the user's language
            source=query.source,
            start_seconds=float(start) if start is not None else None,
            end_seconds=float(end) if end is not None else None,
        )
        video = VideoRef(prepared=prepared, local_path=await memory.local_media_path(), duration_seconds=query.source.duration_seconds)
        try:
            result = await self.understanding.understand(video, request)
        except AppError as exc:
            return {"error": f"Video understanding failed: {exc.message}"}
        trace["escalated"] = True
        trace.setdefault("understanding_providers", []).append(f"{result.provider}:{result.model}")
        usage.input_tokens = (usage.input_tokens or 0) + (result.usage.input_tokens or 0)
        usage.output_tokens = (usage.output_tokens or 0) + (result.usage.output_tokens or 0)
        events.extend(result.events)
        return {
            "evidence_level": "model_interpretation",
            "provider": result.provider,
            "observations": result.description,
            "details": result.observations[:12],
            "confidence": result.confidence,
            "insufficient_evidence": result.insufficient_evidence,
            "timestamps": [t.model_dump() for t in result.timestamps],
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
