"""Agent tools over stored vision data, and persistence through VisionService."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent.tools import ToolError, VisionMemory
from app.db.session import get_sessionmaker
from app.models import ObjectTrack, SceneSnapshot, User, VideoEvent, VideoSourceRecord, VisionRun


async def seed(recorded_start_at: datetime | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    """A 60 s gate video: person #1 in view 5-50 s, person #2 30-40 s, car #3 10-20 s."""
    async with get_sessionmaker()() as db:
        user = User(email=f"{uuid.uuid4().hex}@x.rw", password_hash="x")
        db.add(user)
        await db.flush()
        source = VideoSourceRecord(owner_id=user.id, name="Gate cam", location="irembo", kind="url", uri="https://x/y.mp4", status="ready", source_metadata={"duration_seconds": 60.0}, recorded_start_at=recorded_start_at)
        db.add(source)
        await db.flush()
        run = VisionRun(video_source_id=source.id, status="completed", detector="yolo", weights="yolo26s.pt", tracker="bytetrack", duration_seconds=60.0)
        db.add(run)
        await db.flush()
        for tid, cls, first, last in [(1, "person", 5, 50), (2, "person", 30, 40), (3, "car", 10, 20)]:
            db.add(ObjectTrack(vision_run_id=run.id, video_source_id=source.id, track_id=tid, object_class=cls, first_seen=first, last_seen=last, frames=100, mean_confidence=0.9, max_confidence=0.95))
        for t in range(0, 61):
            ids = [tid for tid, a, b in [(1, 5, 50), (2, 30, 40), (3, 10, 20)] if a <= t <= b]
            classes = Counter("car" if i == 3 else "person" for i in ids)
            db.add(SceneSnapshot(vision_run_id=run.id, timestamp=float(t), counts=dict(classes), track_ids=ids))
        for etype, cls, tid, t, end in [("object_appeared", "person", 1, 5, 50), ("object_appeared", "car", 3, 10, 20), ("object_appeared", "person", 2, 30, 40), ("person_entered", "person", 2, 31, None), ("object_disappeared", "person", 1, 50, None)]:
            db.add(VideoEvent(video_source_id=source.id, vision_run_id=run.id, event_type=etype, evidence_level="rule", object_class=cls, track_id=tid, zone="gate" if etype == "person_entered" else None, description=f"{cls} #{tid} {etype}", start_time=t, end_time=end, detector="local:yolo+bytetrack", event_metadata={"rule": {"name": etype}}))
        await db.commit()
        return user.id, source.id


async def test_counting_and_current_objects(engine: object) -> None:
    user_id, source_id = await seed()
    async with get_sessionmaker()() as db:
        mem = VisionMemory(db, user_id, source_id)
        whole = await mem.count_objects()
        assert whole["distinct_tracks_by_class"] == {"person": 2, "car": 1}
        assert whole["max_simultaneously_visible_by_class"] == {"person": 2, "car": 1}
        assert (await mem.count_objects(object_class="person", start_seconds=0, end_seconds=20))["distinct_tracks_by_class"] == {"person": 1}

        now = await mem.get_current_objects()  # "now" = end of recording: nobody left
        assert now["at_seconds"] == 60.0 and now["objects"] == []
        at35 = await mem.get_current_objects(at_seconds=35)
        assert [o["track_id"] for o in at35["objects"]] == [1, 2]
        assert at35["objects"][0]["seconds_in_view_so_far"] == 30.0


async def test_events_by_range_type_and_last_seconds(engine: object) -> None:
    user_id, source_id = await seed()
    async with get_sessionmaker()() as db:
        mem = VisionMemory(db, user_id, source_id)
        entries = await mem.get_recent_events(event_types=["person_entered"])
        assert [e["track_id"] for e in entries["events"]] == [2] and entries["evidence_level"] == "rule"
        last15 = await mem.get_recent_events(last_seconds=15)  # 45..60 s
        assert {e["type"] for e in last15["events"]} == {"object_appeared", "object_disappeared"}  # track 1 still visible at 45, then leaves at 50
        detail = await mem.get_event_details(entries["events"][0]["event_id"])
        assert detail["rule"] == {"name": "person_entered"} and detail["evidence_level"] == "rule"


async def test_clock_times_require_known_start(engine: object) -> None:
    user_id, source_id = await seed()
    async with get_sessionmaker()() as db:
        with pytest.raises(ToolError, match="no known recording start"):
            await VisionMemory(db, user_id, source_id).get_recent_events(start_clock="14:00", end_clock="14:01")

    # recording starts 13:59:30 Kigali time (UTC+2) -> 14:00 is 30 s into the video
    user_id, source_id = await seed(datetime(2026, 9, 23, 11, 59, 30, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        events = await VisionMemory(db, user_id, source_id).get_recent_events(start_clock="14:00", end_clock="14:00")
        assert events["range_seconds"] == [30.0, 30.0]
        assert [e["track_id"] for e in events["events"]] == [1, 2]  # both visible at 30 s


async def test_camera_resolution_and_isolation(engine: object) -> None:
    user_id, source_id = await seed()
    other_user, other_source = await seed()
    async with get_sessionmaker()() as db:
        mem = VisionMemory(db, user_id, None)
        assert (await mem.get_camera_status("irembo"))["camera_id"] == str(source_id)
        with pytest.raises(ToolError):
            await mem.get_camera_status(str(other_source))  # someone else's camera


async def test_tools_explain_missing_local_analysis(engine: object) -> None:
    async with get_sessionmaker()() as db:
        user = User(email="n@x.rw", password_hash="x")
        db.add(user)
        await db.flush()
        source = VideoSourceRecord(owner_id=user.id, name="YT", kind="url", uri="https://youtu.be/x", status="ready")
        db.add(source)
        await db.flush()
        db.add(VisionRun(video_source_id=source.id, status="failed", detector="yolo", weights="w", tracker="bytetrack", error="YouTube links are analysed by Gemini only."))
        await db.commit()
        with pytest.raises(ToolError, match="analyze_video_clip"):
            await VisionMemory(db, user.id, source.id).count_objects()


async def test_vision_service_persists_pipeline_output(engine: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Runs the service end to end with a fake pipeline (no GPU)."""
    from app.core.config import get_settings
    from app.services import vision as vision_service
    from app.video.gateway import VideoGateway
    from app.video.sources.base import MediaHandle
    from app.vision.pipeline import Snapshot, TrackSummary, VisionPipeline, VisionRunResult
    from app.vision.types import VisionEvent

    monkeypatch.setattr(get_settings(), "vision_enabled", True)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    async def acquire(self, source, window=None):  # type: ignore[no-untyped-def]
        return MediaHandle(mime_type="video/mp4", local_path=video)

    def fake_run(self, path, **kwargs):  # type: ignore[no-untyped-def]
        track = TrackSummary(4, Counter({"person": 10}), first_seen=1.0, last_seen=9.0, frames=10, confidence_sum=9.0, max_confidence=0.95, first_bbox=(1, 2, 3, 4), last_bbox=(5, 6, 7, 8))
        events = [VisionEvent("object_appeared", 1.0, "person #4 appeared in view", confidence=0.9, track_id=4, object_class="person", end_timestamp=9.0, metadata={"rule": {"name": "track_confirmed"}})]
        return VisionRunResult(events=events, tracks={4: track}, snapshots=[Snapshot(1.0, {"person": 1}, [4])], stats={"duration_seconds": 10.0, "processing_fps": 99.0})

    monkeypatch.setattr(VideoGateway, "acquire", acquire)
    monkeypatch.setattr(VisionPipeline, "run", fake_run)
    monkeypatch.setattr(vision_service, "_detector", lambda *a: type("D", (), {"name": "yolo", "weights": "w"})())

    user_id, source_id = await seed(datetime(2026, 9, 23, 12, 0, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        source = await db.get(VideoSourceRecord, source_id)
        run = await vision_service.VisionService(db, VideoGateway(get_settings())).start(source)
    for task in list(vision_service._tasks):
        await task

    async with get_sessionmaker()() as db:
        fresh = await db.get(VisionRun, run.id)
        assert fresh.status == "completed" and fresh.stats["processing_fps"] == 99.0
        mem = VisionMemory(db, user_id, source_id)
        assert (await mem.count_objects())["distinct_tracks_by_class"] == {"person": 1}  # the seeded older run was replaced
        events = (await mem.get_recent_events())["events"]
        assert len(events) == 1 and events[0]["track_id"] == 4
        stored = await db.get(VideoEvent, uuid.UUID(events[0]["event_id"]))
        assert stored.occurred_at.replace(tzinfo=UTC) == datetime(2026, 9, 23, 12, 0, 1, tzinfo=UTC)


async def test_model_comparison_keeps_runs_side_by_side(client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """YOLO and RT-DETR runs coexist; the agent reads the configured (primary) one; boxes are served."""
    from app.core.config import get_settings
    from app.services import vision as vision_service
    from app.video.gateway import VideoGateway
    from app.video.sources.base import MediaHandle
    from app.vision.pipeline import TrackSummary, VisionPipeline, VisionRunResult
    from tests.conftest import register

    monkeypatch.setattr(get_settings(), "vision_enabled", True)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    async def acquire(self, source, window=None):  # type: ignore[no-untyped-def]
        return MediaHandle(mime_type="video/mp4", local_path=video)

    def fake_run(self, path, **kwargs):  # type: ignore[no-untyped-def]
        n = 2 if self.detector.name == "rtdetr" else 1  # pretend RT-DETR finds a duplicate
        tracks = {i: TrackSummary(i, Counter({"car": 5}), first_seen=0, last_seen=4, frames=5, confidence_sum=4.5, max_confidence=0.9) for i in range(1, n + 1)}
        frames = [{"t": 0.0, "o": [[i, "car", 0.9, 10, 10, 50, 50] for i in tracks]}]
        return VisionRunResult(events=[], tracks=tracks, snapshots=[], stats={"duration_seconds": 5.0, "processing_fps": 50.0 * n, "resolution": [640, 360]}, frames=frames)

    monkeypatch.setattr(VideoGateway, "acquire", acquire)
    monkeypatch.setattr(VisionPipeline, "run", fake_run)
    monkeypatch.setattr(vision_service, "_detector", lambda name, *a: type("D", (), {"name": name, "weights": "w"})())
    monkeypatch.setattr(vision_service, "vision_available", lambda settings=None: True)
    from app.api.routes import vision as vision_routes

    monkeypatch.setattr(vision_routes, "vision_available", lambda settings=None: True)

    headers = await register(client)
    async with get_sessionmaker()() as db:
        user = (await db.execute(__import__("sqlalchemy").select(User))).scalars().first()
        source = VideoSourceRecord(owner_id=user.id, name="Gate", kind="url", uri="https://x/y.mp4", status="ready")
        db.add(source)
        await db.commit()
        sid = source.id

    for combo in ({"detector": "yolo", "tracker": "bytetrack"}, {"detector": "rtdetr", "tracker": "bytetrack"}):
        r = await client.post(f"/api/video-sources/{sid}/vision/run", json=combo, headers=headers)
        assert r.status_code == 200, r.text
        for task in list(vision_service._tasks):
            await task

    runs = (await client.get(f"/api/video-sources/{sid}/vision/runs", headers=headers)).json()
    by_label = {r["label"]: r for r in runs}
    assert set(by_label) == {"YOLO26s + ByteTrack", "RT-DETR-L + ByteTrack"}
    assert by_label["YOLO26s + ByteTrack"]["is_primary"] and not by_label["RT-DETR-L + ByteTrack"]["is_primary"]
    assert by_label["RT-DETR-L + ByteTrack"]["tracks_by_class"] == {"car": 2}
    assert by_label["RT-DETR-L + ByteTrack"]["performance"]["processing_fps"] == 100.0

    boxes = (await client.get(f"/api/video-sources/{sid}/vision/runs/{by_label['RT-DETR-L + ByteTrack']['id']}/boxes", headers=headers)).json()
    assert boxes["resolution"] == [640, 360] and len(boxes["frames"][0]["o"]) == 2

    async with get_sessionmaker()() as db:  # the agent answers from the primary (YOLO) run
        assert (await VisionMemory(db, user.id, sid).count_objects())["distinct_tracks_by_class"] == {"car": 1}

    # re-running a combination replaces only that combination
    await client.post(f"/api/video-sources/{sid}/vision/run", json={"detector": "yolo", "tracker": "bytetrack"}, headers=headers)
    for task in list(vision_service._tasks):
        await task
    assert len((await client.get(f"/api/video-sources/{sid}/vision/runs", headers=headers)).json()) == 2


async def test_youtube_sources_explain_instead_of_failing(client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from app.api.routes import vision as vision_routes
    from app.services import vision as vision_service
    from tests.conftest import register

    for module in (vision_service, vision_routes):
        monkeypatch.setattr(module, "vision_available", lambda settings=None: True)
    headers = await register(client)
    r = await client.post("/api/video-sources", json={"name": "yt", "uri": "https://www.youtube.com/watch?v=abc123"}, headers=headers)
    assert r.status_code == 201, r.text  # registering still works; local analysis is simply skipped
    sid = r.json()["id"]

    runs = (await client.get(f"/api/video-sources/{sid}/vision/runs", headers=headers)).json()
    assert runs[0]["status"] == "unsupported" and "upload the video file" in runs[0]["unsupported_reason"]
    r = await client.post(f"/api/video-sources/{sid}/vision/run", json={"detector": "rtdetr", "tracker": "bytetrack"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "local_vision_unsupported"
    assert vision_service._tasks == set()


async def test_retry_replaces_failed_run(engine: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    from app.services import vision as vision_service
    from app.video.gateway import VideoGateway

    async def boom(run_id, gateway):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(vision_service, "_execute", boom)
    _, source_id = await seed()
    async with get_sessionmaker()() as db:
        source = await db.get(VideoSourceRecord, source_id)
        db.add(VisionRun(video_source_id=source_id, status="failed", detector="rtdetr", weights="w", tracker="bytetrack", error="x"))
        await db.commit()
        await vision_service.VisionService(db, VideoGateway(get_settings())).start(source, "rtdetr", "bytetrack")
        runs = (await db.execute(__import__("sqlalchemy").select(VisionRun).where(VisionRun.detector == "rtdetr"))).scalars().all()
        assert [r.status for r in runs] == ["queued"]
