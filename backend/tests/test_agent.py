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
    assert result.trace == {"tools_used": ["count_objects"], "escalated": False, "steps": 2, "evidence_levels": ["tracking"], "agent_llm": "gemini", "agent_model": "fake-model"}
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


async def test_local_llm_agent_over_openai_compatible_api(engine: object, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """AGENT_LLM=local: a Qwen-style model on Ollama drives the same tools through /chat/completions."""
    import json

    import httpx

    user_id, source_id = await seed()
    sent: list[dict] = []
    replies = [
        {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "count_objects", "arguments": json.dumps({"object_class": "person"})}}]}}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}},
        {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "final_answer", "arguments": json.dumps({"answer": "Habonetse abantu babiri.", "confidence": 0.8, "insufficient_evidence": False})}}]}}], "usage": {"prompt_tokens": 80, "completion_tokens": 9}},
    ]

    async def fake_post(self, url, json=None, headers=None, **kw):  # type: ignore[no-untyped-def]
        sent.append(__import__("copy").deepcopy(json))  # snapshot: the chat keeps appending to its message list
        return httpx.Response(200, json=replies.pop(0), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    agent = AgentVideoAnalyzer(video_analyzer=None, model="qwen3-vl:8b", local_llm={"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3-vl:8b", "api_key": None})
    prepared = await agent.prepare(__import__("app.video.sources.base", fromlist=["MediaHandle"]).MediaHandle(mime_type="video/mp4"))
    assert prepared.ref == {"local": True}  # nothing uploaded to Gemini

    result = await agent.analyze(prepared, query(user_id, source_id, "Ni abantu bangahe?"))
    assert result.answer == "Habonetse abantu babiri." and result.trace["agent_llm"] == "local"
    assert result.usage.input_tokens == 130
    assert sent[0]["tool_choice"] == "required" and sent[0]["messages"][0]["role"] == "system"
    tool_msg = sent[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and '"person": 2' in tool_msg["content"]


async def test_local_llm_plain_text_answer_is_accepted_after_a_nudge(engine: object, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    user_id, source_id = await seed()
    replies = [{"choices": [{"message": {"content": "Hari abantu babiri."}}]}] * 2

    async def fake_post(self, url, json=None, headers=None, **kw):  # type: ignore[no-untyped-def]
        return httpx.Response(200, json=replies[0], request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    agent = AgentVideoAnalyzer(video_analyzer=None, model="m", local_llm={"base_url": "http://x/v1", "model": "m", "api_key": None})
    result = await agent.analyze(PreparedVideo(analyzer="agent", ref={"local": True}), query(user_id, source_id, "?"))
    assert result.answer == "Hari abantu babiri." and result.trace["unstructured_answer"] is True and result.confidence == 0.3


async def test_local_agent_works_in_english_and_translates(engine: object, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """AGENT_LLM=local + translator: Kinyarwanda question -> English for the model -> Kinyarwanda answer."""
    import json

    import httpx

    from app.agent import analyzer as agent_module

    class FakeTranslator:
        name = "fake_nllb"
        calls: list[tuple[str, str, str]] = []

        async def translate(self, text: str, source: str, target: str) -> str:
            self.calls.append((text, source, target))
            return {"Ni abantu bangahe?": "How many people?", "There are 2 people.": "Hari abantu 2."}.get(text, text)

    monkeypatch.setattr(agent_module.AgentVideoAnalyzer, "_translator", staticmethod(lambda: FakeTranslator()))
    user_id, source_id = await seed()
    seen: list[dict] = []
    replies = [{"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "final_answer", "arguments": json.dumps({"answer": "There are 2 people.", "confidence": 0.8, "insufficient_evidence": False})}}]}}]}]

    async def fake_post(self, url, json=None, headers=None, **kw):  # type: ignore[no-untyped-def]
        seen.append(__import__("copy").deepcopy(json))
        return httpx.Response(200, json=replies.pop(0), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    agent = AgentVideoAnalyzer(video_analyzer=None, model="qwen2.5:7b", local_llm={"base_url": "http://x/v1", "model": "qwen2.5:7b", "api_key": None})
    result = await agent.analyze(PreparedVideo(analyzer="agent", ref={"local": True}), query(user_id, source_id, "Ni abantu bangahe?"))

    assert seen[0]["messages"][-1]["content"].startswith("How many people?")  # the model saw English
    assert "Write `answer` in English" in seen[0]["messages"][0]["content"]
    assert result.answer == "Hari abantu 2." and result.language == "rw"
    assert result.trace["answer_en"] == "There are 2 people." and result.trace["translated_by"] == "fake_nllb"


async def test_local_stt_short_recording_is_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Near-silent/too-short recordings give an empty transcript (the route then asks to try again)."""
    import numpy as np

    from app.voice import local_stt

    monkeypatch.setattr(local_stt, "decode_audio", lambda audio: np.zeros(1000, dtype=np.float32))
    stt = local_stt.MMSSpeechToText()
    result = await stt.transcribe(b"x", "audio/webm", "rw")
    assert result.text == "" and result.provider == "mms" and stt._model is None  # model not even loaded


def test_inline_final_answer_text_is_recovered() -> None:
    from app.agent.llm import inline_calls

    text = 'Based on the video...\n\nfinal_answer({"answer":"9 elephants at once.","confidence":0.8,"evidence":[]})'
    (call,) = inline_calls(text)
    assert call.name == "final_answer" and call.args["answer"] == "9 elephants at once."
    (call,) = inline_calls('{"name": "final_answer", "arguments": {"answer": "yes"}}')
    assert call.args == {"answer": "yes"}
    assert inline_calls("There are 9 elephants.") == []
