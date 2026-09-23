"""Conversation state carried between turns.

Two layers of memory:
1. Recent history: the last N question/answer pairs are replayed verbatim to
   the analyzer, which makes references like "umwe muri bo" resolvable.
2. Rolling state (stored in conversations.context): entities under
   discussion, focus timestamps and recent events. It survives after old
   turns fall out of the history window.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.analyzers.base import AnalysisResult, ConversationTurn
from app.models import ConversationMessage

HISTORY_TURNS = 8  # question/answer pairs replayed to the analyzer
_MAX_ENTITIES = 12
_MAX_EVENTS = 8


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class ConversationState(BaseModel):
    entities: list[str] = Field(default_factory=list)
    focus_timestamps: list[float] = Field(default_factory=list)
    recent_events: list[str] = Field(default_factory=list)
    turns: int = 0

    @classmethod
    def load(cls, data: dict[str, Any] | None) -> ConversationState:
        return cls.model_validate(data or {})

    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.entities:
            notes.append("Things discussed so far: " + "; ".join(self.entities))
        if self.focus_timestamps:
            notes.append("The previous answer referred to: " + ", ".join(_fmt_time(t) for t in self.focus_timestamps))
        if self.recent_events:
            notes.append("Events noted earlier: " + "; ".join(self.recent_events))
        return notes

    def update(self, result: AnalysisResult) -> None:
        self.turns += 1
        for entity in result.referenced_entities:
            if entity in self.entities:
                self.entities.remove(entity)
            self.entities.append(entity)
        self.entities = self.entities[-_MAX_ENTITIES:]
        if result.timestamps:
            self.focus_timestamps = [t.start_seconds for t in result.timestamps[:5]]
        for event in result.events:
            self.recent_events.append(f"{event.event_type.value} at {_fmt_time(event.start_seconds)}: {event.description}")
        self.recent_events = self.recent_events[-_MAX_EVENTS:]


def build_history(messages: list[ConversationMessage]) -> list[ConversationTurn]:
    """Answered question/answer pairs, oldest first, capped at HISTORY_TURNS."""
    turns: list[ConversationTurn] = []
    pending_question: ConversationMessage | None = None
    for message in messages:
        if message.role == "user":
            pending_question = message
        elif message.role == "assistant" and pending_question is not None and not message.message_metadata.get("error"):
            turns.append(ConversationTurn(role="user", content=pending_question.content))
            turns.append(
                ConversationTurn(
                    role="assistant",
                    content=message.content,
                    timestamps=[t["start_seconds"] for t in message.timestamps if "start_seconds" in t],
                )
            )
            pending_question = None
    return turns[-HISTORY_TURNS * 2:]
