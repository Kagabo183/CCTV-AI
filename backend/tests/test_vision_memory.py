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
    monkeypatch.setattr(vision_service, "_detector", lambda *a, **k: type("D", (), {"name": "yolo", "weights": "w"})())

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
    monkeypatch.setattr(vision_service, "_detector", lambda name, *a, **k: type("D", (), {"name": name, "weights": "w"})())
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


async def test_youtube_source_is_analysed_from_its_stream(client, monkeypatch: pytest.MonkeyPatch, fake_import: list[str]) -> None:  # type: ignore[no-untyped-def]
    """YouTube links are analysed in place (from the online stream); "import" no longer downloads them."""
    from app.api.routes import vision as vision_routes
    from app.services import vision as vision_service
    from tests.conftest import register, wait_imports

    for module in (vision_service, vision_routes):
        monkeypatch.setattr(module, "vision_available", lambda settings=None: True)
    monkeypatch.setattr(vision_service.VisionService, "ensure", lambda self, source: _noop())
    headers = await register(client)
    async with get_sessionmaker()() as db:
        user = (await db.execute(__import__("sqlalchemy").select(User))).scalars().first()
        legacy = VideoSourceRecord(owner_id=user.id, name="watch", kind="url", uri="https://www.youtube.com/watch?v=1gi5qn1khVk", status="ready", source_metadata={"delivery": "youtube", "youtube_id": "1gi5qn1khVk"})
        db.add(legacy)
        await db.commit()
        sid = legacy.id

    runs = (await client.get(f"/api/video-sources/{sid}/vision/runs", headers=headers)).json()
    assert all(r["status"] != "unsupported" for r in runs)  # local runs are possible without a stored copy

    source = (await client.post(f"/api/video-sources/{sid}/import", headers=headers)).json()
    await wait_imports()
    assert source["status"] == "ready" and source["playback"]["type"] == "youtube" and source["metadata"]["analysis"] == "stream"
    assert "stored_path" not in source["metadata"] and fake_import == []  # nothing was downloaded


async def _noop() -> None:
    return None


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


async def test_tracks_and_describe_with_vlm(client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Uncertain tracks are listed honestly; 'describe' asks a VLM and stores its answer as model_interpretation."""
    from app.understanding import factory
    from app.understanding.base import UnderstandingResult
    from tests.conftest import register

    headers = await register(client)
    async with get_sessionmaker()() as db:
        user = (await db.execute(__import__("sqlalchemy").select(User))).scalars().first()
        source = VideoSourceRecord(owner_id=user.id, name="Bunny", kind="upload", uri="x.mp4", status="ready", source_metadata={})
        db.add(source)
        await db.flush()
        run = VisionRun(video_source_id=source.id, status="completed", detector="yolo", weights="yolo26s.pt", tracker="bytetrack", duration_seconds=60)
        db.add(run)
        await db.flush()
        db.add(ObjectTrack(vision_run_id=run.id, video_source_id=source.id, track_id=7, object_class="unknown", first_seen=60, last_seen=67, frames=70,
                           mean_confidence=0.41, max_confidence=0.66, track_metadata={"candidate_class": "person", "uncertain": True, "class_votes": {"person": 70}, "last_bbox": [1, 2, 3, 4]}))
        await db.commit()
        sid, rid = source.id, run.id

    tracks = (await client.get(f"/api/video-sources/{sid}/vision/runs/{rid}/tracks", headers=headers)).json()
    assert tracks[0]["object_class"] == "unknown" and tracks[0]["candidate_class"] == "person" and tracks[0]["uncertain"]

    asked: list[str] = []

    class FakeVLM:
        name, model = "fake_vlm", "fake-1"

        async def understand(self, video, request):  # type: ignore[no-untyped-def]
            asked.append(request.question)
            assert (request.start_seconds, request.end_seconds) == (59.5, 67.5)
            return UnderstandingResult(description="A large white cartoon rabbit, not a person.", confidence=0.9, provider="fake_vlm", model="fake-1")

    monkeypatch.setattr(factory, "get_understanding_provider", lambda: FakeVLM())
    r = await client.post(f"/api/video-sources/{sid}/vision/runs/{rid}/tracks/7/describe", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["description"].startswith("A large white cartoon rabbit") and r.json()["evidence_level"] == "model_interpretation"
    assert "guessed it is a 'person'" in asked[0]

    events = (await client.get(f"/api/video-sources/{sid}/events?vision_run_id={rid}", headers=headers)).json()
    assert [(e["evidence_level"], e["track_id"]) for e in events] == [("model_interpretation", 7)]
    tracks = (await client.get(f"/api/video-sources/{sid}/vision/runs/{rid}/tracks", headers=headers)).json()
    assert tracks[0]["object_class"] == "unknown"  # the detector's own label is never overwritten


async def test_queue_position_cancel_and_restart_recovery(client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Queued runs say what they wait for, can be cancelled, and nothing hangs forever after a restart."""
    from datetime import timedelta

    from app.api.routes import vision as vision_routes
    from app.core.config import get_settings
    from app.models import VideoSession
    from app.services import vision as vision_service
    from app.video.gateway import VideoGateway
    from tests.conftest import register

    monkeypatch.setattr(vision_routes, "vision_available", lambda settings=None: True)
    headers = await register(client)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as db:
        user = (await db.execute(__import__("sqlalchemy").select(User))).scalars().first()
        long_video = VideoSourceRecord(owner_id=user.id, name="Long HLS", kind="url", uri="https://x/a.m3u8", status="ready")
        mine = VideoSourceRecord(owner_id=user.id, name="My upload", kind="upload", uri="u.mp4", status="ready")
        stuck_import = VideoSourceRecord(owner_id=user.id, name="Cam", kind="rtsp", uri="rtsp://x/1", status="importing")
        db.add_all([long_video, mine, stuck_import])
        await db.flush()
        running = VisionRun(video_source_id=long_video.id, status="running", detector="yolo_o365", weights="w", tracker="bytetrack", created_at=now - timedelta(minutes=2))
        q1 = VisionRun(video_source_id=mine.id, status="queued", detector="yolo", weights="w", tracker="bytetrack", created_at=now - timedelta(minutes=1))
        q2 = VisionRun(video_source_id=mine.id, status="queued", detector="rtdetr", weights="w", tracker="bytetrack", created_at=now)
        db.add_all([running, q1, q2, VideoSession(video_source_id=mine.id, user_id=user.id, analyzer="agent", status="preparing")])
        await db.commit()
        ids = (mine.id, q1.id, q2.id)

    runs = {r["detector"]: r for r in (await client.get(f"/api/video-sources/{ids[0]}/vision/runs", headers=headers)).json()}
    assert runs["yolo"]["queue_position"] == 1 and runs["rtdetr"]["queue_position"] == 2
    assert runs["yolo"]["waiting_for"].startswith("Long HLS (yolo_o365+bytetrack)")

    r = await client.post(f"/api/video-sources/{ids[0]}/vision/runs/{ids[2]}/cancel", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

    counts = await vision_service.recover_after_restart(VideoGateway(get_settings()))
    assert counts["interrupted"] == 1 and counts["sessions"] == 1 and counts["imports"] == 1
    async with get_sessionmaker()() as db:
        assert (await db.get(VisionRun, running.id)).status == "queued"  # interrupted runs start again
        assert (await db.get(VisionRun, ids[1])).status == "queued"  # re-queued (not started in tests: vision disabled)
        cam = (await db.execute(__import__("sqlalchemy").select(VideoSourceRecord).where(VideoSourceRecord.name == "Cam"))).scalar_one()
        assert cam.status == "error" and "Try again" in cam.status_message
