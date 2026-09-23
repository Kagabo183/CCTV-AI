from __future__ import annotations

import json

import pytest

from app.analyzers.base import AnalysisQuery, AnalysisResult, ConversationTurn, PreparedVideo, SourceContext
from app.analyzers.gemini import GeminiVideoAnalyzer, normalize_response
from app.conversation.context import ConversationState, build_history
from app.conversation.orchestrator import resolve_source_mention
from app.core.errors import ProviderUnavailable
from app.events.types import EventType, normalize_event_type
from app.models import ConversationMessage, VideoSourceRecord

GEMINI_JSON = {
    "answer": "Kuri camera yo ku irembo hari abantu babiri n'imodoka imwe.",
    "confidence": 1.4,
    "insufficient_evidence": False,
    "timestamps": [{"start_seconds": 40, "end_seconds": 55, "label": "car"}, {"start_seconds": 12, "label": "people"}],
    "events": [
        {"event_type": "vehicle_detected", "description": "White car stops at gate", "start_seconds": 40, "confidence": 0.9},
        {"event_type": "car parked", "description": "Car parked", "start_seconds": 41},
    ],
    "evidence": [{"description": "Two people near the gate", "timestamp_seconds": 12}],
    "referenced_entities": ["two people near gate", "white car"],
}


def test_normalize_response_is_provider_neutral() -> None:
    result = normalize_response(json.dumps(GEMINI_JSON), model="gemini-x", language="rw")
    assert isinstance(result, AnalysisResult)
    assert result.confidence == 1.0  # clamped
    assert [t.start_seconds for t in result.timestamps] == [12, 40]  # sorted
    assert result.events[0].event_type is EventType.VEHICLE_DETECTED
    assert result.events[1].event_type is EventType.OBSERVATION
    assert result.events[1].metadata == {"provider_label": "car parked"}
    assert result.referenced_entities == ["two people near gate", "white car"]


def test_normalize_rejects_garbage() -> None:
    with pytest.raises(ProviderUnavailable):
        normalize_response("not json", model=None, language="rw")


def test_event_type_normalization() -> None:
    assert normalize_event_type("Person Entered") is EventType.PERSON_ENTERED
    assert normalize_event_type("car") is EventType.VEHICLE_DETECTED
    assert normalize_event_type(None) is EventType.OBSERVATION


def test_gemini_prompt_puts_video_first_and_keeps_history() -> None:
    analyzer = GeminiVideoAnalyzer(api_key="test-key", model="gemini-test")
    prepared = PreparedVideo(analyzer="gemini", ref={"file_uri": "https://generativelanguage.googleapis.com/v1beta/files/abc", "mime_type": "video/mp4"})
    query = AnalysisQuery(
        question="Umwe muri bo yinjiye?",
        source=SourceContext(name="Gate", location="irembo", kind="url"),
        history=[
            ConversationTurn(role="user", content="Hari abantu bangahe?"),
            ConversationTurn(role="assistant", content="Hari abantu babiri.", timestamps=[12.0]),
        ],
        context_notes=["Things discussed so far: two people near gate"],
    )
    contents = analyzer.build_contents(prepared, query)
    assert contents[0].parts[0].file_data.file_uri.endswith("/files/abc")
    assert "irembo" in contents[0].parts[1].text and "two people near gate" in contents[0].parts[1].text
    assert [c.role for c in contents] == ["user", "model", "user", "model", "user"]
    assert "12s" in contents[3].parts[0].text
    assert contents[-1].parts[0].text == "Umwe muri bo yinjiye?"


def _msg(role: str, content: str, **kw: object) -> ConversationMessage:
    return ConversationMessage(role=role, content=content, timestamps=kw.get("timestamps", []), message_metadata=kw.get("meta", {}))


def test_history_skips_failed_turns() -> None:
    messages = [
        _msg("user", "q1"),
        _msg("assistant", "a1", timestamps=[{"start_seconds": 5.0}]),
        _msg("user", "q2 failed", meta={"failed": True}),
        _msg("user", "q3"),
        _msg("assistant", "a3"),
    ]
    turns = build_history(messages)
    assert [(t.role, t.content) for t in turns] == [("user", "q1"), ("assistant", "a1"), ("user", "q3"), ("assistant", "a3")]
    assert turns[1].timestamps == [5.0]


def test_state_accumulates_entities() -> None:
    state = ConversationState()
    state.update(normalize_response(json.dumps(GEMINI_JSON), model=None, language="rw"))
    notes = " ".join(state.notes())
    assert "white car" in notes and "0:12" in notes and "vehicle_detected" in notes


def test_source_mention_resolution() -> None:
    gate = VideoSourceRecord(name="Camera 1", location="irembo")
    parking = VideoSourceRecord(name="Parking", location="parikingi")
    import uuid

    gate.id, parking.id = uuid.uuid4(), uuid.uuid4()
    assert resolve_source_mention("Ni iki kiri kuba kuri camera yo ku irembo?", parking, [gate, parking]) is gate
    assert resolve_source_mention("Hari abantu bangahe?", parking, [gate, parking]) is None
    assert resolve_source_mention("Reba irembo", gate, [gate, parking]) is None  # already selected
