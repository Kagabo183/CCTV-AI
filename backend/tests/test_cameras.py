"""Camera platform: credential handling, stream tokens, media auth, ONVIF addresses, gateway probes, live questions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.cameras import credentials as vault
from app.cameras import media_server
from app.cameras.connections import ChannelInfo, ProbeResult, StreamProfile
from app.cameras.diagnostics import Check, Diagnosis, ProbeReport
from app.cameras.onvif import device_url


def test_split_and_redact_credentials() -> None:
    url, user, pw = vault.split_credentials("rtsp://admin:Cam%232026!@10.0.0.5:554/Streaming/Channels/101")
    assert (url, user, pw) == ("rtsp://10.0.0.5:554/Streaming/Channels/101", "admin", "Cam#2026!")
    assert vault.with_credentials(url, user, pw) == "rtsp://admin:Cam%232026%21@10.0.0.5:554/Streaming/Channels/101"
    text = "could not open rtsp://admin:secret@cam.local/x and http://u:p@nvr/onvif"
    assert "secret" not in vault.redact(text) and ":p@" not in vault.redact(text)
    assert vault.redact(text).count("***@") == 2


def test_credentials_are_encrypted() -> None:
    token = vault.encrypt({"username": "admin", "password": "Cam#2026!"})
    assert "Cam#2026" not in token and "admin" not in token
    assert vault.decrypt(token) == {"username": "admin", "password": "Cam#2026!"}
    with pytest.raises(ValueError):
        vault.decrypt(token[:-4] + "AAAA")


def test_stream_token_is_scoped_to_one_camera_and_action() -> None:
    user, cam, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    path = media_server.camera_path(cam, "sub")
    token = media_server.stream_token(user, path, ("read",))
    assert media_server.check_stream_token(token, path, "read") == str(user)
    assert media_server.check_stream_token(token, path, "publish") is None
    assert media_server.check_stream_token(token, media_server.camera_path(other, "sub"), "read") is None
    assert media_server.check_stream_token(token, path + "x", "read") is None  # no prefix tricks
    expired = media_server.stream_token(user, path, ("read",), ttl=timedelta(seconds=-1))
    assert media_server.check_stream_token(expired, path, "read") is None


async def test_media_auth_hook(engine: object) -> None:
    from app.cameras.access import authorize_media, secret_hash
    from app.db.session import get_sessionmaker
    from app.models import Gateway, User

    async with get_sessionmaker()() as db:
        owner = User(email="o@example.com", password_hash="x")
        db.add(owner)
        await db.flush()
        gw = Gateway(owner_id=owner.id, name="g", status="online", secret_hash=secret_hash("vgs_secret"))
        db.add(gw)
        await db.commit()
        cam = uuid.uuid4()
        path = media_server.camera_path(cam, "main")
        token = media_server.stream_token(owner.id, path)
        assert await authorize_media(db, {"action": "read", "path": path, "token": token})
        assert await authorize_media(db, {"action": "read", "path": path, "query": f"token={token}"})
        assert not await authorize_media(db, {"action": "read", "path": path})
        assert not await authorize_media(db, {"action": "publish", "path": path, "token": token})
        assert await authorize_media(db, {"action": "read", "path": path, "user": media_server.INTERNAL_USER, "password": media_server.internal_password()})
        mine = media_server.gateway_path(gw.id, cam)
        assert await authorize_media(db, {"action": "publish", "path": mine, "user": gw.id.hex, "password": "vgs_secret"})
        assert not await authorize_media(db, {"action": "publish", "path": mine, "user": gw.id.hex, "password": "wrong"})
        other = media_server.gateway_path(uuid.uuid4(), cam)  # another gateway's namespace
        assert not await authorize_media(db, {"action": "publish", "path": other, "user": gw.id.hex, "password": "vgs_secret"})
        gw.status = "revoked"
        await db.commit()
        assert not await authorize_media(db, {"action": "publish", "path": mine, "user": gw.id.hex, "password": "vgs_secret"})


def test_onvif_device_url() -> None:
    assert device_url("192.168.1.20") == "http://192.168.1.20/onvif/device_service"
    assert device_url("192.168.1.20:8080") == "http://192.168.1.20:8080/onvif/device_service"
    assert device_url("http://cam.local/onvif/device_service") == "http://cam.local/onvif/device_service"


def test_gateway_probe_round_trip() -> None:
    report = ProbeReport([Check("reachable", True, "10.0.0.5", ms=3.0), Check("authentication", False, "401", Diagnosis.AUTHENTICATION_FAILED)])
    result = ProbeResult(report, {"h264": True}, [ChannelInfo("1", "Channel 1", [StreamProfile("main", "rtsp://10.0.0.5/1", "H.264", 1920, 1080, 25.0)])], {"model": "X"})
    again = ProbeResult.from_dict(result.as_dict())
    assert again.as_dict() == result.as_dict()
    assert again.report.failure.code is Diagnosis.AUTHENTICATION_FAILED
    assert not again.ok


def test_network_policy_blocks_unlisted_private_networks_and_own_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cameras.service import check_network_allowed
    from app.core.config import get_settings
    from app.core.errors import ValidationFailed

    monkeypatch.setattr(get_settings(), "camera_private_networks", ["192.168.1.0/24", "127.0.0.0/8"])
    check_network_allowed("rtsp://192.168.1.20/stream")
    check_network_allowed("rtsp://127.0.0.1:18554/stream")
    for url in ("rtsp://10.1.2.3/stream", "http://169.254.169.254/latest", f"rtsp://127.0.0.1:{get_settings().media_rtsp_port}/cam/x/main", "http://127.0.0.1:8000/api"):
        with pytest.raises(ValidationFailed):
            check_network_allowed(url)


@pytest.mark.parametrize(("question", "now", "past"), [
    ("What is happening on Camera 3?", True, None),
    ("What is going on at the gate now?", True, None),
    ("Is anyone there?", True, None),
    ("What happened in the last 20 minutes?", False, 20),
    ("Anything in the past hour?", False, 1),
    ("What is the man wearing?", False, None),
])
def test_live_question_routes(question: str, now: bool, past: int | None) -> None:
    from app.agent.analyzer import _LIVE_NOW, _LIVE_PAST, _NUMBER_WORDS, _OPEN_QUESTION

    open_q = bool(_OPEN_QUESTION.search(question))
    m = _LIVE_PAST.search(question)
    assert (bool(_LIVE_NOW.search(question)) and not open_q and m is None) == now
    if past is None:
        assert m is None or open_q
    else:
        amount = (m.group(2) or "one").lower()
        assert (int(amount) if amount.isdigit() else _NUMBER_WORDS[amount]) == past


def test_live_time_range() -> None:
    from app.agent.tools import ToolError, VisionMemory

    start, end = VisionMemory._wall_range({"last_seconds": 1200}, 3600)
    assert abs((end - start).total_seconds() - 1200) < 1 and abs((datetime.now(UTC) - end).total_seconds()) < 2
    start, end = VisionMemory._wall_range({"start_clock": "00:00", "end_clock": "00:01"}, 3600)
    assert (end - start) == timedelta(minutes=1)
    with pytest.raises(ToolError):
        VisionMemory._wall_range({"start_seconds": 10}, 3600)  # video seconds make no sense on a live camera


def test_kinyarwanda_time_and_people_phrases() -> None:
    from app.language.glossary import rw_terms_to_english as pre

    assert pre("Ni iki cyabaye mu minota icumi ishize?") == "Ni iki cyabaye in the last 10 minutes?"
    assert pre("Ni iki cyabaye mu minota 20 ishize?") == "Ni iki cyabaye in the last 20 minutes?"
    assert pre("Ni iki cyabaye mu isaha ishize?") == "Ni iki cyabaye in the last hour?"
    assert pre("Ni iki cyabaye mu masaha abiri ashize?") == "Ni iki cyabaye in the last 2 hours?"
    assert pre("Hari abantu bangahe?") == "Hari people bangahe?"
    assert pre("Inzovu yaje ryari?") == "elephant yaje ryari?"


def test_camera_event_classifier() -> None:
    from app.cameras.device_events import _classify

    motion = {"topic": "tns1:RuleEngine/CellMotionDetector/Motion", "operation": "Changed", "data": {"IsMotion": "true"}}
    assert _classify(motion)[0] == "camera_motion"
    assert _classify({**motion, "data": {"IsMotion": "false"}}) is None  # motion ended: not an event
    assert _classify({**motion, "operation": "Initialized", "data": {"IsMotion": "false"}}) is None
    assert _classify({"topic": "tns1:VideoSource/GlobalSceneChange/ImagingService", "data": {"State": "true"}}) is not None
    assert _classify({"topic": "tns1:RuleEngine/TamperDetector/Tamper", "data": {"IsTamper": "true"}})[0] == "camera_tamper"
    assert _classify({"topic": "tns1:RuleEngine/LineDetector/Crossed", "data": {"ObjectId": "3"}})[0] == "camera_line_crossed"
    assert _classify({"topic": "tns1:Device/Trigger/DigitalInput", "operation": "Initialized", "data": {"LogicalState": "false"}}) is None


def test_capability_profile_only_claims_what_exists() -> None:
    from types import SimpleNamespace

    from app.api.routes.cameras import capability_profile

    onvif = SimpleNamespace(kind="camera_onvif", gateway_id=None, ai_profile={"record": True},
                            source_metadata={"capabilities": {"audio": True, "onvif_profiles": ["S", "T"]}, "device": {"services": {"ptz": "http://x/ptz", "events": "http://x/ev"}},
                                             "streams": [{"role": "main", "codec": "H.264"}, {"role": "sub", "codec": "H.264"}]})
    caps = capability_profile(onvif)
    assert caps["ptz"] and caps["events"] and caps["substream"] and caps["h264"] and not caps["h265"] and not caps["talk"]
    rtsp = SimpleNamespace(kind="camera_rtsp", gateway_id=None, ai_profile={"record": False}, source_metadata={"streams": [{"role": "main", "codec": "H.265"}]})
    caps = capability_profile(rtsp)
    assert not caps["ptz"] and not caps["events"] and not caps["recording"] and caps["h265"] and not caps["substream"]


@pytest.mark.parametrize(("question", "kind"), [
    ("Are there any animals?", "animals"),
    ("What animals are on the camera?", "animals"),
    ("When did the elephant come?", "arrival"),
    ("How many people are there?", "count"),
    ("What's on the door camera?", "now"),
])
def test_live_question_kinds(question: str, kind: str) -> None:
    from app.agent.analyzer import _LIVE_ANIMALS, _LIVE_ARRIVAL, _LIVE_COUNT, _LIVE_NOW

    got = {"animals": bool(_LIVE_ANIMALS.search(question)), "arrival": bool(_LIVE_ARRIVAL.search(question)),
           "count": bool(_LIVE_COUNT.search(question)), "now": bool(_LIVE_NOW.search(question))}
    assert got[kind]


def test_token_links_are_recognised() -> None:
    from app.cameras.rtsp import _has_access_token

    assert _has_access_token("rtsp://rtsp.rtsplink.com/live/retail?token=f1eff08")
    assert _has_access_token("rtsp://cam.example.com/stream?sig=abc&st=123")
    assert not _has_access_token("rtsp://192.168.1.20:554/Streaming/Channels/101")
    assert not _has_access_token("rtsp://cam/live?channel=1&subtype=0")
