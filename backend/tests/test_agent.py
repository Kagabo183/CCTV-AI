"""Agent loop with a scripted fake LLM: tool dispatch, escalation, evidence."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from google.genai import types

from app.agent.analyzer import AgentVideoAnalyzer
from app.analyzers.base import AnalysisQuery, AnalysisResult, DetectedEvent, PreparedVideo, SourceContext, TimeReference
from app.events.types import EventType
from tests.test_vision_memory import seed


def call(name: str, **args: Any) -> SimpleNamespace:
    fc = types.FunctionCall(name=name, args=args)
    return SimpleNamespace(
        function_calls=[fc],
        candidates=[SimpleNamespace(content=types.Content(role="model", parts=[types.Part(function_call=fc)]))],
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=10),
    )


class FakeLLM:
    def __init__(self, script: list[SimpleNamespace]) -> None:
        self.script = list(script)
        self.requests: list[list[types.Content]] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self.generate))

    async def generate(self, *, model: str, contents: list[types.Content], config: Any) -> SimpleNamespace:
        self.requests.append(list(contents))
        return self.script.pop(0)


class FakeVideo:
    """Stands in for GeminiVideoAnalyzer on escalation."""

    def __init__(self) -> None:
        self._client = None
        self.queries: list[AnalysisQuery] = []

    async def analyze(self, prepared: PreparedVideo, query: AnalysisQuery) -> AnalysisResult:
        self.queries.append(query)
        return AnalysisResult(
            answer="A man in a red jacket walks through the gate carrying a black bag.",
            confidence=0.8,
            timestamps=[TimeReference(start_seconds=31)],
            events=[DetectedEvent(event_type=EventType.PERSON_ENTERED, description="man with bag enters", start_seconds=31)],
            analyzer="gemini",
        )


def make_agent(script: list[SimpleNamespace]) -> tuple[AgentVideoAnalyzer, FakeLLM, FakeVideo]:
    video = FakeVideo()
    agent = AgentVideoAnalyzer(video_analyzer=video, model="fake-model")  # type: ignore[arg-type]
    llm = FakeLLM(script)
    agent._client = llm
    return agent, llm, video


def query(user_id: Any, source_id: Any, question: str) -> AnalysisQuery:
    return AnalysisQuery(question=question, user_id=user_id, source=SourceContext(source_id=source_id, name="Gate cam", location="irembo", kind="url", duration_seconds=60))


async def test_local_answer_without_escalation(engine: object) -> None:
    user_id, source_id = await seed()
    agent, llm, video = make_agent([
        call("count_objects", object_class="person"),
        call("final_answer", answer="Habonetse abantu babiri.", confidence=0.85, insufficient_evidence=False,
             timestamps=[{"start_seconds": 30, "label": "both visible"}], evidence=[{"description": "2 person tracks", "level": "tracking"}]),
    ])
    result = await agent.analyze(PreparedVideo(analyzer="agent", ref={"file_uri": "f"}), query(user_id, source_id, "Ni abantu bangahe?"))

    assert result.answer == "Habonetse abantu babiri." and result.analyzer == "agent"
    assert result.trace == {"tools_used": ["count_objects"], "escalated": False, "steps": 2, "evidence_levels": ["tracking"]}
    assert video.queries == []  # no video tokens spent
    assert result.usage.input_tokens == 200
    tool_reply = llm.requests[1][-1]  # the tool result was fed back to the model
    assert tool_reply.role == "user" and tool_reply.parts[0].function_response.response["result"]["distinct_tracks_by_class"] == {"person": 2}


async def test_escalates_to_video_model_on_a_window(engine: object) -> None:
    user_id, source_id = await seed()
    agent, llm, video = make_agent([
        call("get_recent_events", event_types=["person_entered"]),
        call("analyze_video_clip", question="What is the person entering carrying?", start_seconds=29, end_seconds=36),
        call("final_answer", answer="Umugabo wambaye ikoti ritukura yinjiye afite igikapu cy'umukara.", confidence=0.75, insufficient_evidence=False,
             timestamps=[{"start_seconds": 31}], evidence=[{"description": "entry at 31 s", "level": "rule"}, {"description": "carrying a black bag", "level": "model_interpretation"}]),
    ])
    result = await agent.analyze(PreparedVideo(analyzer="agent", ref={"file_uri": "f"}), query(user_id, source_id, "Uwinjiye yari afite iki?"))

    assert result.trace["tools_used"] == ["get_recent_events", "analyze_video_clip"] and result.trace["escalated"] is True
    assert video.queries[0].window.start == 29 and video.queries[0].window.end == 36
    assert video.queries[0].language == "en"
    assert [e.evidence_level for e in result.events] == ["model_interpretation"]
    assert result.trace["evidence_levels"] == ["model_interpretation", "rule"]


async def test_tool_errors_are_returned_to_the_model(engine: object) -> None:
    user_id, source_id = await seed()
    agent, llm, _ = make_agent([
        call("get_recent_events", start_clock="14:00"),  # no recording start time known
        call("final_answer", answer="Iyi video nta saha izwi ifite.", confidence=0.9, insufficient_evidence=True),
    ])
    result = await agent.analyze(PreparedVideo(analyzer="agent", ref={}), query(user_id, source_id, "Ni iki cyabaye saa munani?"))
    assert "no known recording start" in llm.requests[1][-1].parts[0].function_response.response["result"]["error"]
    assert result.insufficient_evidence is True


async def test_last_step_forces_final_answer(engine: object) -> None:
    user_id, source_id = await seed()
    agent, llm, _ = make_agent([call("list_cameras"), call("final_answer", answer="ok", confidence=0.5, insufficient_evidence=False)])
    agent.max_steps = 2
    configs: list[Any] = []
    original = llm.generate

    async def spy(*, model: str, contents: list[types.Content], config: Any) -> SimpleNamespace:
        configs.append(config)
        return await original(model=model, contents=contents, config=config)

    llm.aio.models.generate_content = spy
    await agent.analyze(PreparedVideo(analyzer="agent", ref={}), query(user_id, source_id, "?"))
    assert configs[0].tool_config.function_calling_config.allowed_function_names is None
    assert configs[1].tool_config.function_calling_config.allowed_function_names == ["final_answer"]
