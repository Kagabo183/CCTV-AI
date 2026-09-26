# Camera platform: connectivity, live video and live AI

## Phase 1: inventory of the existing system (before any change)

| Area | What exists (Sep 2026) |
|---|---|
| **Video ingestion** | `VideoSourceRecord` (`video_sources`), with `kind` in `url` \| `upload` \| `local` \| `rtsp`. `app/video/ingest.py` imports links as constant-frame-rate H.264 MP4 files. YouTube links are analysed in place from the stream (`app/video/streaming.py`). |
| **Camera support** | An "RTSP camera" today is a **one-off clip** recorded by ffmpeg. `onvif` and `nvr` are stubs that raise "not supported yet" (`app/video/sources/live.py`). There is no live view, no status monitoring and no stored credentials: passwords are stripped from the URI before saving. |
| **Detector** | `app/vision/detectors.py`: YOLO26s (COCO), with tiling for small objects, YOLO26s-Objects365 and RT-DETR-L. Wildlife uses MegaDetector V6, with SpeciesNet run per track (`app/wildlife`). |
| **Tracker** | ByteTrack or BoT-SORT (`app/vision/trackers.py`). |
| **Event engine** | `app/vision/events.py`. Covers zones, lines, dwell, loitering, crowds, and animal rules (restricted areas, infrastructure, groups, fast movement). |
| **Pipeline** | `VisionPipeline.run(path_or_url)` processes a **finite** file or stream and produces a run's tracks, snapshots, events and per-frame boxes. |
| **Database** | Alembic migrations 0001 and 0002. Tables: `users`, `video_sources`, `video_sessions`, `video_events`, `vision_runs`, `object_tracks`, `scene_snapshots`, `conversations`, `messages`, `analysis_requests`. |
| **APIs** | `/api/auth/*`, `/api/video-sources/*` (including uploads, imports and vision runs), `/api/conversations/*` (text and voice), `/api/system/*`. |
| **Frontend** | Next.js 16. One workspace page: camera list, video, insights, wildlife and the assistant. No live player. |
| **Authentication** | JWT in an httpOnly cookie, or a Bearer token. Every source has one owner; there is no sharing. |
| **GPU** | One RTX PRO 4000 (24 GB). Vision runs are serialised by an asyncio lock, with one resident detector at a time. Ollama serves qwen2.5:7b and qwen3-vl. NLLB handles translation and MMS handles speech-to-text. |

## Target architecture (implemented incrementally; see "Status" below)

```
                         VISIONARY SERVER
   Auth/users ─── Camera Service ─── AI Agent (tools: cameras, live state, events, wildlife)
                     │   │
                     │   ├── CameraConnection providers: RTSP · ONVIF · HLS/HTTP · NVR · Gateway · Vendor (Imou…)
                     │   ├── Credential vault (Fernet-encrypted, never returned by the API)
                     │   └── Health monitor (heartbeat, exponential-backoff reconnect)
                     │
                 Media server (MediaMTX): RTSP pull from cameras / RTSP push from gateways
                     │  → WebRTC (WHEP) + HLS to browsers and mobile · recording (fMP4) · playback API
                     │  every read/publish is authorised by Visionary (/api/media/auth, short-lived tokens)
                     │
                 Live AI workers: sub-stream → detector (general or wildlife, per camera AI profile)
                     → tracker → event engine → video_events (occurred_at wall clock) + live state

   Customer LAN:  cameras ── RTSP/ONVIF ── Visionary Gateway ── OUTBOUND WSS (control) + RTSP push (media)
```

Design rules:

- **Cameras are video sources.** A camera is a `video_sources` row with a live kind (`camera_rtsp`, `camera_onvif`,
  `camera_hls`, `camera_vendor`, `nvr` + `nvr_channel`), so every existing feature works on cameras too: the agent, events, runs and the UI. No
  parallel "cameras" table duplicates it.
- **Identity is stable.** Each organisation (currently the owner user), gateway, camera and channel has its own
  UUID. IP addresses are only connection details.
- **Credentials** live only in `camera_credentials`, encrypted with Fernet. The key comes from
  `CAMERA_CREDENTIALS_KEY` or is derived from `SECRET_KEY`. The API, the logs and the browser never see them;
  RTSP URLs are redacted before any log line.
- **Browsers never talk RTSP.** MediaMTX re-publishes each stream as WebRTC (WHEP) or HLS. A viewer gets a stream
  token that is valid for 5 minutes and for one camera path only.
- **Remote cameras do not need port forwarding.** The gateway dials out to the server (WebSocket control plus RTSP
  publish). The server never connects into the customer network.

## Status (26 Sep 2026)

Implemented and tested against **simulated** cameras. No physical camera, NVR or remote site has been tested yet.

| Part | Where | Notes |
|---|---|---|
| Connection providers | `app/cameras/connections.py`, `rtsp.py`, `onvif.py`, `vendors.py` | RTSP (DESCRIBE with Digest/Basic, ffmpeg probe, a frame read), ONVIF (WS-Security digest, Media2/Profile T first then Media1, stream + snapshot URIs, capabilities), NVR (every channel becomes a camera), HLS. Imou: official Open Platform API, **untested** (no developer account). |
| Discovery | `onvif.discover` | WS-Discovery on each local interface, **only interfaces inside `CAMERA_PRIVATE_NETWORKS`** (or run by a gateway on its own LAN). |
| Diagnosis | `diagnostics.py` | 16 codes, each with a plain explanation and a next step (wrong vs missing password, stream path, port closed, DNS, not an allowed network, gateway offline…). |
| Credentials | `credentials.py`, `camera_credentials` | Fernet-encrypted. Never returned, logged or written to the media server config (paths are added through its local API). |
| Stream gateway | `media_server.py` (MediaMTX 1.21.1) | Pulls each camera once; WebRTC (WHEP) and HLS to browsers; fMP4 recording + playback; every read/publish authorised by `/api/media/auth`. H.265-only cameras get an on-demand H.264 copy for browsers without H.265 WebRTC. The media server and all ffmpeg children die with the backend (Windows job object / Linux PDEATHSIG). |
| Access | `access.py`, `camera_shares`, `audit_logs` | Owner / admin / operator / viewer + per-share permissions (live_view, playback, download, ptz, audio, talk, ai_query, camera_settings). 5-minute stream tokens scoped to one camera path. Audit log for adds, deletes, credential changes, shares, live views, gateway enrolment and revocation. |
| Health | `service.py` | Media-server path state every 5 s; a camera that is down is re-diagnosed with exponential backoff (heartbeat × 2ⁿ, max 5 min) and re-registered when it answers. States: online · connecting · offline · auth_failed · stream_error · gateway_offline · disconnected. |
| Live AI | `live.py` | One worker per camera: detector per AI profile (general YOLO26s and/or wildlife MegaDetector + SpeciesNet per track) → ByteTrack → event engine → `video_events` with wall-clock `occurred_at`, 10 s scene snapshots, finished tracks. The current picture is kept in memory (also used for instant snapshots). |
| Gateway | `gateway_hub.py`, `gateway_agent/visionary_gateway.py` | One-time enrolment code (hashed, 60 min) → gateway id + secret (hashed). Outbound WebSocket control (probe, discover, start/stop streams, heartbeat). Media pushed with `ffmpeg -c copy` to `gw/<gateway>/…`; the hub never connects into the site. |
| Assistant | `app/agent/tools.py`, `analyzer.py` | Live cameras are answered in Kigali clock time. "What is happening (on Camera 3)?" and "…in the last N minutes?" are answered straight from the live AI (no LLM tool guessing). Other questions go through the agent; a visual question records a fresh 8 s clip for the video AI. Shared cameras need `ai_query`. |
| UI | `frontend/src/components/cameras/*`, `NavRail.tsx` | Cameras (status grid, attention list), camera page (WebRTC live + AI boxes, right now, activity, recordings, connection, settings and sharing), Add-camera wizard (connect → address → test with every check → capabilities/channels → name & AI), Live wall (1 / 2×2 / 3×3), Events, Recordings, Gateways; live cameras also play in the Assistant. Phone layout: bottom tabs, no horizontal scroll at 390 px. |

Not built yet: PTZ control, two-way audio, ONVIF event subscription, TLS for the RTSP push from gateways (MediaMTX
supports RTSPS; not wired), TURN server provisioning, Analytics and Settings pages, multi-organisation tenancy
(owner = user today).

## Acceptance results

`python tools/camsim/camsim.py` runs a camera-side MediaMTX with looping H.264/H.265 publishers plus an ONVIF
simulator ("Hikvision DS-2CD2143G2-I", Profile S+T with Media2; "Dahua NVR4104-4KS2", 4 channels).
`python scripts/camera_acceptance.py` drives the real API. Latest run: **66 / 66 passed**
(`bench/camera_acceptance.json`). Everything ran on one machine: "remote" and "behind NAT" are simulated.

| # | Test | Result |
|---|---|---|
| 1 | RTSP camera | Added in one call; live through the media server 0.2 s after saving. |
| 2 | ONVIF camera | Profile S+T, Media2, main 1920×1080 + sub 640×360 found automatically; live after 1.1 s. |
| 3 | H.264 | WebRTC in the browser (Edge) and with aiortc: the server answers WHEP in 0.25 s, first frame 0.9 s later. aiortc's own ICE gathering on this Windows host adds ~5 s to its measured 5.8 s. |
| 4 | H.265 | Pulled and recorded as H.265. Browsers without H.265 WebRTC get the on-demand H.264 copy: first frame after 11 s (transcoder start). |
| 5 | Remote camera via gateway | Enrolment (the code works once), gateway online in 0.5 s, camera probed by the gateway on its LAN, stream pushed (`rtspSession`: the hub never connected to the camera), live WebRTC. Gateway power loss → `gateway_offline` in 5 s; online again 6.5 s after it returns; revoking disconnects it. |
| 6 | Connection loss | Camera stopped → `stream_error` / STREAM_NOT_FOUND in 5.5 s; online again 6.2 s after it returns. |
| 7 | NVR | 4 channels → 4 cameras, all live. |
| 8 | Wildlife stream | MegaDetector + SpeciesNet live: events like "plains zebra appeared in view". |
| 9 | "What is happening on Camera 3?" | Asked while another camera is selected: switches to Camera 3 and answers from the live AI in 0.1 s (English) / 13 s (Kinyarwanda, including translation). |
| 10 | "The last 20 minutes" | Peak simultaneous counts + events in Kigali time; 0.1 s (English) / 2 s (Kinyarwanda). |
| 11 | Multiple simultaneous streams | 8 cameras live + 3 WebRTC viewers at once; see the performance table for 24. |
| 12 | Security | No password in API responses, `dev.db`, backend log, media server log or config; no internal media secret in logs; unauthenticated, foreign or expired tokens refused; other users get 404; the viewer role cannot change settings; Visionary's own ports and non-allowed private networks are refused as camera addresses. |

Also covered: 8 diagnosis cases (OK, INVALID_CREDENTIALS, AUTHENTICATION_FAILED, STREAM_NOT_FOUND, RTSP_DISABLED,
DNS_FAILED, ONVIF wrong password, ONVIF_DISABLED), discovery (both simulated devices in 3.1 s), snapshots,
recording + playback (10 s of recorded 1080p returned as MP4 in 1.4 s). Unit tests: `tests/test_cameras.py`.

## Performance (RTX PRO 4000 24 GB, 24-core CPU)

`python scripts/camera_perf.py` (results in `bench/camera_perf.json`). Live AI target: 2 frames/s per camera,
`LIVE_AI_STREAM=auto` (main stream when the sub-stream is below 720p), `LIVE_AI_TILING=off`.

| Cameras with live AI | AI frames/s per camera | ms per frame | GPU util | Backend CPU (of machine) | Backend RAM |
|---|---|---|---|---|---|
| 8 | 1.8–1.9 | 18–47 | 9 % | 10 % | 4.7 GB |
| 24 | 1.7–2.05 | 12–112 | 22 % | 29 % | 5.7 GB |

The media server stays under 1 % CPU (it copies packets; it only transcodes for the on-demand H.265 copy).
With tiled detection (`LIVE_AI_TILING=on`, ~10 inferences per frame), 11 cameras on 1080p main streams reached only
~1.1 frames/s each at ~850 ms per frame: tiling is for a few cameras where small, distant objects matter.

## Honest limits

- Only simulated devices were tested. Real cameras differ (firmware quirks, partial ONVIF, UDP-only RTSP,
  vendor-specific paths); the connection test and diagnosis codes are there to explain what a real device does.
- "Behind NAT" was simulated on one machine: the gateway only makes outbound connections, but no real NAT, firewall
  or Internet latency was involved. Viewers outside the LAN need STUN/TURN (`MEDIA_ICE_SERVERS`); no TURN server is
  provisioned.
- WebRTC was verified in Edge (Chromium) and aiortc; not yet on Safari/iOS or Firefox.
- The ONVIF simulator implements the operations Visionary uses (device information, services, capabilities, scopes,
  profiles, stream and snapshot URIs, Digest + WS-Security), not the whole standard.
- The general detector mislabels animals it does not know (hippos came out as "person" on an NVR channel with only
  the general profile); turn on the wildlife profile for wildlife cameras.
- SQLite foreign keys were not enforced before this work, so deleted sources had left orphan rows. Enforcement is now
  on for every connection and the dev database was cleaned (backup: `backend/dev.db.bak-fk`).

## Running it

```
cd backend
python tools/camsim/camsim.py                     # simulated cameras (optional)
# .env: CAMERA_PRIVATE_NETWORKS=127.0.0.0/8 only for the simulator; your camera LAN (e.g. 192.168.1.0/24) in real use
uvicorn app.main:app --port 8000                  # also starts MediaMTX (tools/mediamtx) and restores cameras
python scripts/camera_acceptance.py               # acceptance run
python scripts/camera_perf.py --seconds 60        # load measurement
```

Gateway on a remote site (Python + this repository's `backend` folder):
`python -m gateway_agent.visionary_gateway enroll --hub https://your-server --token vge_…`, then `… run`.

## Upgrade pass (26 Sep 2026): from camera connection to camera platform

### Architecture map (inspected before changing anything)

| Area | Where | State before this pass |
|---|---|---|
| Camera pages | `frontend/src/components/cameras/*` (grid, camera page, live wall, events, recordings, gateways), `NavRail.tsx` | Working. |
| Add Camera wizard | `AddCameraWizard.tsx`: Connection → Address → Test → Name & AI | Four options (find on network, ONVIF, RTSP, NVR/DVR); location limited to "same network" or a gateway. |
| Camera tables | `video_sources` (a camera is a source with a live kind, NVR channels have `parent_id`), `camera_credentials`, `camera_shares`, `gateways`, `stream_sessions`, `audit_logs` | No duplication needed; nothing new added in this pass. |
| Camera APIs | `app/api/routes/cameras.py` | CRUD, test, discover, live ticket, snapshot, events, recordings, playback, shares, gateways, media auth. |
| ONVIF / RTSP / NVR | `app/cameras/onvif.py`, `rtsp.py`, `connections.py` | Discovery, device info, Media2/Media1 profiles, stream URIs, capabilities. No PTZ, no events. |
| Video ingestion / normalisation | MediaMTX (`media_server.py`) pulls or receives every stream once; everything downstream reads from it | Working. |
| Player | `LivePlayer.tsx` (WebRTC/WHEP) | Working; no stats, audio always muted. |
| AI | `live.py` workers → detector (per AI profile) → ByteTrack → `EventEngine` → `video_events` | One GPU call per camera frame. |
| Events | `video_events` with `occurred_at` | No bounding box, species or clip reference. |
| Auth | JWT cookie + roles/permissions per camera | Working. |

### What this pass added

- **Where is the camera?** The wizard's first step now asks *where* before *how*: same network · another site through
  a Visionary Gateway (recommended) · Internet stream address (public ONVIF/RTSP/HLS, with a warning about exposing
  cameras) · manufacturer's cloud (Imou; shown as needing configuration until an Imou developer app is set up). The
  four original options stay; "Web stream link (HLS)" appears for Internet streams. Advanced settings hold the
  sub-stream link. Discovery shows model, IP, "ONVIF detected", Profile S/T and "Already added".
- **ONVIF PTZ**: ContinuousMove / Stop / GetPresets / GotoPreset / GetStatus, only when the device has a PTZ service;
  `GET/POST /api/cameras/{id}/ptz` (needs the `ptz` permission). Camera page: hold-to-move pad and presets.
- **ONVIF events**: pull-point subscription per camera (`app/cameras/device_events.py`), renewed every 9 minutes,
  reconnect with backoff. Motion, tamper, line-crossing and intrusion alarms become events with evidence "device"
  (shown as "camera" in the UI), separate from Visionary's AI events.
- **Capability profile** (`GET /api/cameras/{id}/capabilities`): video, audio, talk, ptz, recording, playback, events,
  snapshot, h264, h265, substream, ai. The UI only shows controls for what is true (talk is false: two-way audio is
  not implemented). `GET /api/cameras/{id}/streams` shows which stream each job uses (live, phones, AI, recording).
- **Diagnostics on a connected camera** (`GET /api/cameras/{id}/diagnostics`): network, ONVIF, login, RTSP, stream,
  codec, resolution, frame rate, start-up time, stability, audio, PTZ, "Visionary is receiving video", AI processing,
  camera event subscription, plus the viewer's own connection (round-trip time, frame rate, packet loss, relayed or
  not) from WebRTC statistics. `POST /api/cameras/{id}/refresh` re-reads what the device supports.
- **Reconnect history** persisted per camera: `reconnect_count`, `last_disconnect`, `last_reconnect`.
- **Camera page**: connection-quality badge, audio on/off (when the camera has audio), snapshot download, full screen,
  PTZ, playback by date and time, and every activity item opens its evidence (recording from 5 s before to 15 s after).
- **Dashboard**: LIVE badges, counts per camera, and a "Recent AI events" feed across cameras (`GET /api/cameras-events`).
- **AI profile per camera**: people, vehicles, other objects, wildlife + species, behaviour events (zones, loitering,
  crowds), recording. Only what is on runs.
- **Event memory**: events now carry the object's bounding box, species (or the uncertain candidate) and a clip
  reference into the recording.
- **Assistant on live cameras**: "Are there any animals?", "When did the elephant arrive?", "How many people are
  there?" are answered from live data (first/last sighting times in Kigali time; "unknown animal" when the species
  is not confident). Kinyarwanda phrases NLLB got wrong are fixed before translation ("mu minota icumi ishize" had
  become "ten minutes ago", "abantu" was dropped), and a count answer that NLLB turned into "the number of dead
  people" was rephrased. The brief's example questions all route to the camera data (1–9 s).
- **Connectors** (`GET /api/cameras-connectors`): ONVIF, RTSP, NVR, HLS and Gateway available; Imou through its
  official Open Platform (needs configuration, unverified); Hikvision, Dahua, Uniview, Axis, Reolink, TP-Link Tapo
  "via standard", i.e. added as ONVIF/RTSP cameras. No brand-specific connector was faked.
- **Batched inference**: live workers submit frames to one inference thread per detector, which batches whatever
  arrives within 10 ms (up to 16). With 24 cameras this averaged 2.1 frames per GPU call.
- **Robustness**: SQLite now runs in WAL mode with a 30 s busy timeout. Removing 16 cameras while 25 were analysing
  had failed with "database is locked"; it now completes (16–19 s).

### Results

`scripts/camera_acceptance.py`: **86 / 86** (the 66 earlier checks + PTZ move/stop/preset, PTZ refused without
permission, ONVIF motion events stored, diagnostics, discovery details, capability profile, stream usage, connector
list, device addresses not exposed, event bounding boxes + clips, an event opening its recording, batching, and seven
new assistant questions in English and Kinyarwanda). Unit tests: 118.

Load (`scripts/camera_perf.py`, 24 general-AI cameras + 1 wildlife camera, 2 frames/s target): median 1.9 frames/s
per camera (min 1.5), median 89 ms per frame, GPU 24 %, backend CPU 34 % of the machine, 5.5 GB RAM, media server
under 1 % CPU. Batching reduced GPU calls by half but did **not** raise throughput here: at 24 % utilisation the GPU
was not the bottleneck (frame decoding on the CPU is the larger cost).

### Still not done

Two-way audio (talk), ONVIF metadata streams, playback from the camera's SD card or an NVR's own storage (ONVIF
Profile G), camera-side events through a gateway, a provisioned TURN server, Imou verified with a real account, and
testing with physical cameras.
