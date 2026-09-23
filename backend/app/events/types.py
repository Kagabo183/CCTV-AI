"""Video event vocabulary.

The enum is the shared language between detectors (Gemini today, the local
engine later), the database and the conversation layer. Being in this list
does NOT mean a type is detected automatically: `DETECTED_BY` records which
analyzers can actually produce each type. In Phase 1 only Gemini produces
events, and only as a by-product of answering a user's question.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    PERSON_DETECTED = "person_detected"
    VEHICLE_DETECTED = "vehicle_detected"
    PERSON_ENTERED = "person_entered"
    PERSON_EXITED = "person_exited"
    VEHICLE_ENTERED = "vehicle_entered"
    VEHICLE_EXITED = "vehicle_exited"
    CROWD_DETECTED = "crowd_detected"
    OBJECT_LEFT = "object_left"
    OBJECT_REMOVED = "object_removed"
    RESTRICTED_ZONE_ENTRY = "restricted_zone_entry"
    UNUSUAL_ACTIVITY = "unusual_activity"
    FIRE = "fire"
    SMOKE = "smoke"
    FALL = "fall"
    LOITERING = "loitering"
    OBSERVATION = "observation"  # anything else the analyzer describes


# Which analyzers can produce which event types. The future local engine will
# register e.g. {"local_engine": {PERSON_DETECTED, VEHICLE_DETECTED, ...}}.
# Types needing zones (RESTRICTED_ZONE_ENTRY) or tracking over time
# (LOITERING, OBJECT_LEFT) are only reliable once cameras/zones exist.
DETECTED_BY: dict[str, frozenset[EventType]] = {
    "gemini": frozenset(EventType),  # best-effort, question-driven, not continuous
    "mock": frozenset({EventType.PERSON_DETECTED, EventType.VEHICLE_DETECTED, EventType.OBSERVATION}),
}

_ALIASES = {
    "person": EventType.PERSON_DETECTED,
    "people": EventType.PERSON_DETECTED,
    "vehicle": EventType.VEHICLE_DETECTED,
    "car": EventType.VEHICLE_DETECTED,
    "person_enter": EventType.PERSON_ENTERED,
    "person_exit": EventType.PERSON_EXITED,
    "vehicle_enter": EventType.VEHICLE_ENTERED,
    "vehicle_exit": EventType.VEHICLE_EXITED,
    "crowd": EventType.CROWD_DETECTED,
}


def normalize_event_type(raw: str | None) -> EventType:
    """Map a provider's free-form label to our vocabulary."""
    if not raw:
        return EventType.OBSERVATION
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    try:
        return EventType(key)
    except ValueError:
        return _ALIASES.get(key, EventType.OBSERVATION)
