"""Event engine rules, driven by synthetic tracks with known geometry."""

from __future__ import annotations

from app.vision.events import EventEngine, EventRules, Line, SceneConfig, Zone
from app.vision.types import EvidenceLevel, TrackedObject

H, W = 100, 100  # frame size; boxes below are in pixels of this frame


def obj(track_id: int, x: float, y: float, t: float, cls: str = "person") -> TrackedObject:
    """A 10x20 box whose bottom-centre anchor is (x, y)."""
    return TrackedObject(track_id, cls, 0.9, (x - 5, y - 20, x + 5, y), frame_number=int(t * 10), timestamp=t)


def run(engine: EventEngine, frames: list[tuple[float, list[TrackedObject]]]) -> list:
    events = []
    for t, objs in frames:
        events += engine.process(objs, t, (H, W))
    return events


def types(events: list) -> list[str]:
    return [e.event_type for e in events]


def test_flicker_is_not_an_object() -> None:
    engine = EventEngine(SceneConfig(rules=EventRules(min_track_seconds=0.5)))
    events = run(engine, [(0.0, [obj(1, 50, 50, 0.0)]), (0.1, []), (5.0, [])])
    assert events == []


def test_appear_then_disappear_with_duration() -> None:
    engine = EventEngine(SceneConfig(rules=EventRules(min_track_seconds=0.5, disappear_after_seconds=2)))
    frames = [(t / 10, [obj(7, 50, 50, t / 10)]) for t in range(0, 31)]  # visible 0..3 s
    frames += [(3.5, []), (5.5, [])]
    events = run(engine, frames)
    assert types(events) == ["object_appeared", "object_disappeared"]
    appeared, gone = events
    assert appeared.timestamp == 0.0 and appeared.end_timestamp == 3.0
    assert gone.timestamp == 3.0 and gone.track_id == 7
    assert all(e.evidence_level is EvidenceLevel.RULE and "rule" in e.metadata for e in events)


def test_zone_entry_exit_and_dwell() -> None:
    gate = Zone("gate", [(0.6, 0.0), (1.0, 0.0), (1.0, 1.0), (0.6, 1.0)])
    engine = EventEngine(SceneConfig(zones=[gate], rules=EventRules(dwell_seconds=3)))
    # walk from x=10 to x=90 (enters the zone at x=60), stay, then walk back out
    frames = [(i * 0.5, [obj(1, 10 + i * 10, 80, i * 0.5)]) for i in range(9)]  # t 0..4, x 10..90
    frames += [(4.0 + i * 0.5, [obj(1, 90, 80, 4.0 + i * 0.5)]) for i in range(1, 7)]  # stay until t=7
    frames += [(7.5, [obj(1, 30, 80, 7.5)])]
    events = [e for e in run(engine, frames) if e.zone == "gate"]
    assert types(events) == ["person_entered", "dwell_in_zone", "person_exited"]
    entered, dwell, exited = events
    assert entered.timestamp == 2.5  # first frame with anchor x >= 60
    assert dwell.metadata["rule"]["measured_seconds"] >= 3
    assert exited.timestamp == 7.5 and exited.metadata["rule"]["seconds_in_zone"] == 5.0


def test_zone_class_filter_and_vehicle_events() -> None:
    lot = Zone("parking", [(0.0, 0.5), (1.0, 0.5), (1.0, 1.0), (0.0, 1.0)], classes={"car"})
    engine = EventEngine(SceneConfig(zones=[lot]))
    # y 20..100: the last frame's box touches the bottom edge and must still count as inside
    frames = [(i * 0.5, [obj(1, 50, 20 + i * 20, i * 0.5, "car"), obj(2, 20, 20 + i * 20, i * 0.5, "person")]) for i in range(5)]
    events = [e for e in run(engine, frames) if e.zone]
    assert types(events) == ["vehicle_entered"] and events[0].track_id == 1


def test_line_crossing_direction() -> None:
    wire = Line("door", (0.0, 0.5), (1.0, 0.5))
    engine = EventEngine(SceneConfig(lines=[wire]))
    down = [(i * 0.5, [obj(1, 50, 30 + i * 10, i * 0.5)]) for i in range(5)]  # y 30 -> 70: moves down the image
    up = [(3 + i * 0.5, [obj(1, 50, 70 - i * 10, 3 + i * 0.5)]) for i in range(5)]
    events = [e for e in run(engine, down + up) if e.zone == "door"]
    assert types(events) == ["person_entered", "person_exited"]


def test_loitering_only_for_people_and_only_once() -> None:
    engine = EventEngine(SceneConfig(rules=EventRules(loiter_seconds=5)))
    frames = [(float(t), [obj(1, 50, 50, float(t)), obj(2, 20, 50, float(t), "car")]) for t in range(12)]
    events = [e for e in run(engine, frames) if e.event_type == "loitering"]
    assert len(events) == 1 and events[0].track_id == 1
    assert events[0].metadata["rule"]["threshold_seconds"] == 5


def test_crowd_threshold_with_hysteresis() -> None:
    engine = EventEngine(SceneConfig(rules=EventRules(crowd_threshold=3, min_track_seconds=0)))

    def people(n: int, t: float) -> list[TrackedObject]:
        return [obj(i, 10 + i * 10, 50, t) for i in range(n)]

    counts = [1, 2, 3, 4, 3, 2, 3, 1, 3]  # dips to 2 do not end the crowd; dropping to 1 does
    frames = [(float(t), people(n, float(t))) for t, n in enumerate(counts)]
    crowd = [e for e in run(engine, frames) if e.event_type == "crowd_detected"]
    # A newly seen person is only confirmed on their second frame (flicker filter),
    # so the third person, first seen at t=2, makes the crowd at t=3.
    assert [e.timestamp for e in crowd] == [3.0, 8.0]
    # the 4th person (t=3 only) is a one-frame flicker and never counts
    assert crowd[0].end_timestamp == 7.0 and crowd[0].metadata["rule"]["max_count"] == 3


def test_scene_config_round_trip() -> None:
    scene = SceneConfig(zones=[Zone("gate", [(0, 0), (1, 0), (1, 1)], {"person"})], lines=[Line("door", (0, 0.5), (1, 0.5))], rules=EventRules(crowd_threshold=9))
    again = SceneConfig.from_dict(scene.to_dict())
    assert again.to_dict() == scene.to_dict()
