"""Event engine: turns tracks into events using explicit geometric/time rules.

Detection and tracking say *what is where*. This layer says *what happened*,
but only things the rules can actually establish from positions over time:

  object_appeared / object_disappeared   a track was confirmed / was lost
  person_entered / person_exited         anchor point crossed into/out of a zone or over a line
  vehicle_entered / vehicle_exited       same, for vehicles
  dwell_in_zone                          stayed inside a zone longer than dwell_seconds
  loitering                              a person stayed in view (or in a zone) longer than loiter_seconds
  crowd_detected                         people in view >= crowd_threshold

Every event records the rule and measured values in `metadata["rule"]`, so an
answer can say *why* it believes something. Nothing here infers intent.
Zone/line coordinates are normalised to [0, 1] so configs survive resolution changes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.vision.types import VEHICLE_CLASSES, EvidenceLevel, TrackedObject, VisionEvent, resolve_label

Point = tuple[float, float]


@dataclass
class Zone:
    name: str
    polygon: list[Point]  # normalised (x, y) vertices
    classes: set[str] | None = None  # None = people and vehicles

    def contains(self, p: Point) -> bool:
        inside = False
        pts = self.polygon
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > p[1]) != (yj > p[1]) and p[0] < (xj - xi) * (p[1] - yi) / (yj - yi + 1e-12) + xi:
                inside = not inside
            j = i
        return inside


@dataclass
class Line:
    """A tripwire. Crossing onto the right-hand side of the direction p1->p2 counts as 'entry'
    (image y grows downward: with p1 on the left and p2 on the right, moving down the image is entry).
    Swap p1 and p2 to reverse."""

    name: str
    p1: Point
    p2: Point

    def side(self, p: Point) -> int:
        cross = (self.p2[0] - self.p1[0]) * (p[1] - self.p1[1]) - (self.p2[1] - self.p1[1]) * (p[0] - self.p1[0])
        return 0 if abs(cross) < 1e-9 else (1 if cross > 0 else -1)

    def spans(self, p: Point, margin: float = 0.05) -> bool:
        """Only count crossings alongside the segment: the point's projection onto
        p1->p2 must fall within the segment (plus a margin), not beyond its ends."""
        (x1, y1), (x2, y2) = self.p1, self.p2
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy or 1e-12
        u = ((p[0] - x1) * dx + (p[1] - y1) * dy) / length_sq
        return -margin <= u <= 1 + margin


@dataclass
class EventRules:
    min_track_seconds: float = 0.5  # a track must persist this long before it "appeared" (filters flicker)
    disappear_after_seconds: float = 2.0
    dwell_seconds: float = 30.0
    loiter_seconds: float = 60.0
    crowd_threshold: int = 5
    # Below this mean confidence (or with an unstable class) a track is "unknown":
    # events describe it as unidentified and person/vehicle rules do not fire.
    confirm_confidence: float = 0.5
    min_class_share: float = 0.6


@dataclass
class SceneConfig:
    zones: list[Zone] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    rules: EventRules = field(default_factory=EventRules)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SceneConfig:
        data = data or {}
        return cls(
            zones=[Zone(z["name"], [tuple(p) for p in z["polygon"]], set(z["classes"]) if z.get("classes") else None) for z in data.get("zones", [])],
            lines=[Line(ln["name"], tuple(ln["p1"]), tuple(ln["p2"])) for ln in data.get("lines", [])],
            rules=EventRules(**data.get("rules", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "zones": [{"name": z.name, "polygon": [list(p) for p in z.polygon], "classes": sorted(z.classes) if z.classes else None} for z in self.zones],
            "lines": [{"name": ln.name, "p1": list(ln.p1), "p2": list(ln.p2)} for ln in self.lines],
            "rules": vars(self.rules),
        }


def _group(object_class: str) -> str | None:
    if object_class == "person":
        return "person"
    if object_class in VEHICLE_CLASSES:
        return "vehicle"
    return None


@dataclass
class _TrackState:
    track_id: int
    object_class: str
    first_seen: float
    last_seen: float
    frames: int = 0
    confidence_sum: float = 0.0
    confirmed: bool = False
    zones_in: dict[str, float] = field(default_factory=dict)  # zone -> entered at
    dwell_reported: set[str] = field(default_factory=set)
    loiter_reported: bool = False
    line_sides: dict[str, int] = field(default_factory=dict)
    appeared_event: VisionEvent | None = None
    votes: Counter = field(default_factory=Counter)
    candidate: str = ""
    uncertain: bool = False

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / max(self.frames, 1)

    @property
    def display(self) -> str:
        """How events name this track: never more certain than the evidence."""
        if self.uncertain:
            return f"unidentified object (possibly {self.candidate}, {self.mean_confidence:.2f})"
        return self.object_class


class EventEngine:
    def __init__(self, scene: SceneConfig | None = None) -> None:
        self.scene = scene or SceneConfig()
        self.rules = self.scene.rules
        self.tracks: dict[int, _TrackState] = {}
        self._crowd_event: VisionEvent | None = None

    # -- main entry ----------------------------------------------------------

    def process(self, tracked: list[TrackedObject], timestamp: float, frame_size: tuple[int, int]) -> list[VisionEvent]:
        """Feed one processed frame. frame_size = (height, width). Returns new events."""
        h, w = frame_size
        events: list[VisionEvent] = []
        for obj in tracked:
            state = self.tracks.get(obj.track_id)
            if state is None:
                state = self.tracks[obj.track_id] = _TrackState(obj.track_id, obj.class_name, timestamp, timestamp)
            state.last_seen = timestamp
            state.frames += 1
            state.confidence_sum += obj.confidence
            state.votes[obj.class_name] += 1
            state.object_class, state.candidate, state.uncertain = resolve_label(
                state.votes, state.mean_confidence, confirm_confidence=self.rules.confirm_confidence, min_class_share=self.rules.min_class_share
            )
            if not state.confirmed and state.frames >= 2 and timestamp - state.first_seen >= self.rules.min_track_seconds:
                state.confirmed = True
                state.appeared_event = self._event(
                    "object_appeared", state.first_seen, state, f"{state.display} #{state.track_id} appeared in view",
                    rule={"name": "track_confirmed", "min_track_seconds": self.rules.min_track_seconds},
                )
                events.append(state.appeared_event)
            if state.confirmed:
                # Clamp inside the frame: boxes touching the bottom edge (people walking
                # toward the camera) must still count as inside zones that reach the edge.
                anchor = (min(max(obj.anchor[0] / w, 0.0), 1 - 1e-6), min(max(obj.anchor[1] / h, 0.0), 1 - 1e-6))
                events += self._zones(state, anchor, timestamp)
                events += self._lines(state, anchor, timestamp)
                events += self._loitering(state, timestamp)
        events += self._disappeared(timestamp)
        events += self._crowd(timestamp)
        return events

    def finish(self, end_timestamp: float) -> list[VisionEvent]:
        """End of media: close open intervals. Tracks still visible are not 'disappeared'."""
        for state in self.tracks.values():
            if state.appeared_event is not None:
                state.appeared_event.end_timestamp = state.last_seen
                state.appeared_event.metadata["visible_at_end"] = True
        if self._crowd_event is not None:
            self._crowd_event.end_timestamp = end_timestamp
        return []

    # -- rules -----------------------------------------------------------------

    def _event(self, event_type: str, timestamp: float, state: _TrackState, description: str, *, zone: str | None = None, rule: dict[str, Any]) -> VisionEvent:
        return VisionEvent(
            event_type=event_type,
            timestamp=timestamp,
            description=description,
            confidence=round(state.mean_confidence, 3),
            track_id=state.track_id,
            object_class=state.object_class,
            zone=zone,
            evidence_level=EvidenceLevel.RULE,
            metadata={"rule": rule, **({"candidate_class": state.candidate, "uncertain": True, "requires_vlm": True} if state.uncertain else {})},
        )

    def _zones(self, state: _TrackState, anchor: Point, t: float) -> list[VisionEvent]:
        events = []
        group = _group(state.object_class)
        for zone in self.scene.zones:
            if zone.classes is not None and state.object_class not in zone.classes:
                continue
            if zone.classes is None and group is None:
                continue
            inside = zone.contains(anchor)
            was_inside = zone.name in state.zones_in
            if inside and not was_inside:
                state.zones_in[zone.name] = t
                if group:
                    events.append(self._event(f"{group}_entered", t, state, f"{state.display} #{state.track_id} entered zone '{zone.name}'", zone=zone.name, rule={"name": "zone_entry", "anchor": "bbox_bottom_center"}))
            elif not inside and was_inside:
                entered = state.zones_in.pop(zone.name)
                state.dwell_reported.discard(zone.name)
                if group:
                    events.append(self._event(f"{group}_exited", t, state, f"{state.display} #{state.track_id} left zone '{zone.name}' after {t - entered:.0f}s", zone=zone.name, rule={"name": "zone_exit", "seconds_in_zone": round(t - entered, 1)}))
            elif inside and zone.name not in state.dwell_reported and t - state.zones_in[zone.name] >= self.rules.dwell_seconds:
                state.dwell_reported.add(zone.name)
                seconds = t - state.zones_in[zone.name]
                events.append(self._event("dwell_in_zone", state.zones_in[zone.name], state, f"{state.display} #{state.track_id} has stayed in zone '{zone.name}' for {seconds:.0f}s", zone=zone.name, rule={"name": "dwell", "threshold_seconds": self.rules.dwell_seconds, "measured_seconds": round(seconds, 1)}))
        return events

    def _lines(self, state: _TrackState, anchor: Point, t: float) -> list[VisionEvent]:
        events = []
        group = _group(state.object_class)
        if group is None:
            return events
        for line in self.scene.lines:
            side = line.side(anchor)
            prev = state.line_sides.get(line.name)
            if side != 0:
                state.line_sides[line.name] = side
            if prev is not None and side != 0 and side != prev and line.spans(anchor):
                direction = "entered" if prev < 0 < side else "exited"
                events.append(self._event(f"{group}_{direction}", t, state, f"{state.display} #{state.track_id} crossed line '{line.name}' ({'entry' if direction == 'entered' else 'exit'} direction)", zone=line.name, rule={"name": "line_crossing", "direction": direction}))
        return events

    def _loitering(self, state: _TrackState, t: float) -> list[VisionEvent]:
        if state.object_class != "person" or state.loiter_reported or t - state.first_seen < self.rules.loiter_seconds:
            return []
        state.loiter_reported = True
        seconds = t - state.first_seen
        return [self._event("loitering", state.first_seen, state, f"person #{state.track_id} has been in view for {seconds:.0f}s", rule={"name": "time_in_view", "threshold_seconds": self.rules.loiter_seconds, "measured_seconds": round(seconds, 1)})]

    def _disappeared(self, t: float) -> list[VisionEvent]:
        events = []
        for track_id in [tid for tid, s in self.tracks.items() if t - s.last_seen > self.rules.disappear_after_seconds]:
            state = self.tracks.pop(track_id)
            if not state.confirmed:
                continue
            if state.appeared_event is not None:
                state.appeared_event.end_timestamp = state.last_seen
            group = _group(state.object_class)
            for zone_name, entered in state.zones_in.items():
                if group:
                    events.append(self._event(f"{group}_exited", state.last_seen, state, f"{state.display} #{track_id} was last seen in zone '{zone_name}' ({state.last_seen - entered:.0f}s inside) and then lost from view", zone=zone_name, rule={"name": "zone_exit_by_track_loss"}))
            events.append(self._event("object_disappeared", state.last_seen, state, f"{state.display} #{track_id} left the view after {state.last_seen - state.first_seen:.0f}s", rule={"name": "track_lost", "after_seconds": self.rules.disappear_after_seconds}))
        return events

    def _crowd(self, t: float) -> list[VisionEvent]:
        people = sum(1 for s in self.tracks.values() if s.confirmed and s.object_class == "person" and s.last_seen == t)
        threshold = self.rules.crowd_threshold
        if self._crowd_event is None and people >= threshold:
            self._crowd_event = VisionEvent(
                event_type="crowd_detected",
                timestamp=t,
                description=f"{people} people in view (threshold {threshold})",
                object_class="person",
                metadata={"rule": {"name": "crowd_threshold", "threshold": threshold, "count": people}},
            )
            return [self._crowd_event]
        if self._crowd_event is not None:
            self._crowd_event.metadata["rule"]["max_count"] = max(people, self._crowd_event.metadata["rule"].get("max_count", 0))
            if people < threshold - 1:  # hysteresis: avoid flapping around the threshold
                self._crowd_event.end_timestamp = t
                self._crowd_event = None
        return []
