"""Events reported BY the camera (ONVIF pull point), next to Visionary's own AI events.

For every ONVIF camera whose device offers an event service: subscribe (CreatePullPointSubscription), pull
messages every few seconds, renew the subscription before it expires, and reconnect with exponential backoff.
Known topics become plain events; anything else is kept as a generic "camera_event" with its topic.

These are the camera's claims (its own motion detector, tamper alarm, line/field rules), stored with
evidence level "device" and detector "onvif", so the assistant and the UI never mix them up with Visionary's
detections.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from app.db.session import get_sessionmaker
from app.models import VideoEvent, VideoSourceRecord

logger = logging.getLogger(__name__)
RENEW_SECONDS = 540  # subscriptions are requested for 600 s
_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
_status: dict[uuid.UUID, dict[str, Any]] = {}


def _classify(msg: dict[str, Any]) -> tuple[str, str] | None:
    """(event_type, description) for a pull-point message, or None when it is not worth storing."""
    topic = msg.get("topic", "")
    data = {k.lower(): str(v).lower() for k, v in (msg.get("data") or {}).items()}
    active = next((v for k, v in data.items() if k in ("ismotion", "state", "istamper", "istampered", "isinside", "alarm")), None)
    if "motion" in topic.lower():
        if active == "true":
            return "camera_motion", "Motion (camera's own detector)"
        return None  # motion ended / initial "no motion": not an event
    if "tamper" in topic.lower():
        return ("camera_tamper", "Tampering: camera covered, moved or out of focus") if active == "true" else None
    if "linedetector" in topic.lower() or "crossed" in topic.lower():
        return "camera_line_crossed", "Line crossed (camera's own rule)"
    if "fielddetector" in topic.lower() or "intrusion" in topic.lower():
        return ("camera_intrusion", "Intrusion zone entered (camera's own rule)") if active in (None, "true") else None
    if msg.get("operation") == "Initialized":
        return None  # initial property states are not events
    return "camera_event", f"The camera reported: {topic.split('/')[-1] or topic}"


def status(source_id: uuid.UUID) -> dict[str, Any]:
    return dict(_status.get(source_id, {}))


def start(source_id: uuid.UUID) -> None:
    stop(source_id)
    _tasks[source_id] = asyncio.get_running_loop().create_task(_listen(source_id))


def stop(source_id: uuid.UUID) -> None:
    task = _tasks.pop(source_id, None)
    if task is not None:
        task.cancel()
    _status.pop(source_id, None)


def stop_all() -> None:
    for sid in list(_tasks):
        stop(sid)


def wanted(source: VideoSourceRecord) -> bool:
    services = (source.source_metadata.get("device") or {}).get("services") or {}
    return source.kind == "camera_onvif" and bool(services.get("events")) and not source.gateway_id and source.connection_state != "disconnected"


async def _listen(source_id: uuid.UUID) -> None:
    from app.cameras.onvif import OnvifClient, OnvifError
    from app.cameras.service import credentials_for

    backoff = 5.0
    st = _status.setdefault(source_id, {"state": "starting", "events": 0, "failures": 0})
    while True:
        try:
            async with get_sessionmaker()() as db:
                source = await db.get(VideoSourceRecord, source_id)
                if source is None or not wanted(source):
                    st["state"] = "off"
                    return
                user, password = await credentials_for(db, source)
                device = source.source_metadata.get("device") or {}
            client = OnvifClient(device["xaddr"], user, password, timeout=15.0)
            await client.sync_clock()
            pull = await client.create_pull_point(device["services"]["events"])
            st.update(state="subscribed", since=datetime.now(UTC).isoformat())
            backoff = 5.0
            renew_at = asyncio.get_running_loop().time() + RENEW_SECONDS
            while asyncio.get_running_loop().time() < renew_at:
                messages = await client.pull_messages(pull, timeout=5)
                stored = [m for m in messages if _classify(m)]
                if stored:
                    await _store(source_id, stored)
                    st["events"] += len(stored)
                st["last_pull"] = datetime.now(UTC).isoformat()
                if not messages:
                    await asyncio.sleep(1.0)  # devices that answer PullMessages at once (no long poll)
        except asyncio.CancelledError:
            raise
        except (OnvifError, OSError, KeyError, ValueError) as exc:
            st.update(state="retrying", error=str(getattr(exc, "detail", exc))[:160], failures=st.get("failures", 0) + 1)
            logger.info("ONVIF events for %s: %s (retry in %.0f s)", source_id, st["error"], backoff)
            await asyncio.sleep(backoff)
            backoff = min(300.0, backoff * 2)


async def _store(source_id: uuid.UUID, messages: list[dict[str, Any]]) -> None:
    async with get_sessionmaker()() as db:
        for m in messages:
            kind, text = _classify(m)  # type: ignore[misc]
            try:
                at = datetime.fromisoformat((m.get("utc") or "").replace("Z", "+00:00"))
            except ValueError:
                at = datetime.now(UTC)
            db.add(VideoEvent(video_source_id=source_id, event_type=kind, evidence_level="device", description=text, occurred_at=at,
                              detector="onvif", event_metadata={"topic": m.get("topic"), "source": m.get("source"), "data": m.get("data")}))
        await db.commit()
