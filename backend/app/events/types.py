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
    OBJECT_APPEARED = "object_appeared"
    OBJECT_DISAPPEARED = "object_disappeared"
    DWELL_IN_ZONE = "dwell_in_zone"
    OBSERVATION = "observation"  # anything else the analyzer describes


# Which analyzers can produce which event types. Types not listed for
# "local_engine" (fire, smoke, fall, object_left, ...) have no local rule yet:
# only a vision-language model can report them, as model_interpretation.
DETECTED_BY: dict[str, frozenset[EventType]] = {
    "gemini": frozenset(EventType),  # best-effort, question-driven, not continuous
    "mock": frozenset({EventType.PERSON_DETECTED, EventType.VEHICLE_DETECTED, EventType.OBSERVATION}),
    # Local rule engine (app/vision/events.py): geometric/time rules over tracks, continuous.
    "local_engine": frozenset({
        EventType.OBJECT_APPEARED, EventType.OBJECT_DISAPPEARED, EventType.PERSON_ENTERED, EventType.PERSON_EXITED,
        EventType.VEHICLE_ENTERED, EventType.VEHICLE_EXITED, EventType.DWELL_IN_ZONE, EventType.LOITERING, EventType.CROWD_DETECTED,
    }),
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
