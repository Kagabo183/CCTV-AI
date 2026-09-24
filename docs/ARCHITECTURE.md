# Architecture

```
User (voice/text, Kinyarwanda)
  → Speech-to-Text (voice/)
  → ConversationOrchestrator (conversation/)       history, rolling context, camera resolution
  → VideoAnalyzer = AgentVideoAnalyzer (agent/)    tool-calling LLM (Gemini, text only)
       ├─ local tools → VisionMemory               events / tracks / scene snapshots in Postgres
       │                  ↑ written by the local vision engine (vision/):
       │                    ObjectDetector (YOLO | RT-DETR) → ObjectTracker (ByteTrack | BoT-SORT) → EventEngine
       └─ analyze_video_clip → GeminiVideoAnalyzer  escalation: deep understanding of a clip
  → answer + timestamps + evidence (each tagged with its evidence level)
  → Text-to-Speech → user
```

The user never talks to YOLO or ByteTrack. The agent chooses tools. The local engine runs once per recorded video (continuously per stream once live cameras arrive), and Gemini only sees video when the agent escalates.

## Local vision engine (`backend/app/vision/`)

| Layer | Module | Output | Evidence level |
|---|---|---|---|
| Detection | `detectors.py`: `ObjectDetector` → `YOLODetector` (yolo26s, default) / `RTDETRDetector` | boxes, classes, confidence per frame | `detection` |
| Tracking | `trackers.py`: `ObjectTracker` → `ByteTrackTracker` (default) / `BoTSORTTracker` | stable track ids over time | `tracking` |
| Events | `events.py`: `EventEngine` + `SceneConfig` (zones, tripwires, thresholds) | appeared/left, zone or tripwire entry/exit, dwell, loitering (= long time in view), crowd | `rule` |
| Pipeline | `pipeline.py` | tracks, 1 Hz scene snapshots, events, performance stats | |
| Service | `services/vision.py` | runs in a worker thread, one job at a time on the GPU; stores `vision_runs`, `object_tracks`, `scene_snapshots`, `video_events` | |

Choose the detector and tracker with `VISION_DETECTOR` and `VISION_TRACKER`. They are alternatives, never run together. The benchmark behind the defaults is in [VISION_BENCHMARK.md](VISION_BENCHMARK.md). Zones are configured per camera through `PUT /api/video-sources/{id}/scene`, in normalised coordinates. Changing them re-runs the analysis.

**Evidence levels** are stored on every event and returned with every answer. The agent's prompt forbids turning `detection`/`tracking`/`rule` evidence into claims about intent or behaviour. Only `model_interpretation` (a vision-language model's description) can describe actions, and it is presented as such. COCO has no classes for fire, smoke, weapons or falls, so those event types stay model-only until a fine-tuned detector exists.

## Conversational agent (`backend/app/agent/`)

Tools (`tools.py`): `list_cameras`, `get_camera_status`, `get_current_objects`, `get_recent_events`, `count_objects`, `get_event_details`, `retrieve_video_clip`, `analyze_video_clip` (escalation), and `final_answer`. Time ranges accept seconds, "last N seconds", or Kigali clock times. Clock times work only when a source has `recorded_start_at`. For recorded video, "now" is the end of the recording.

Escalation happens when the question needs appearance or actions, when local analysis is unavailable (still processing, failed, or a YouTube link the server cannot download), or when the user asks for detail. It sends only the relevant window, using Gemini `VideoMetadata.start_offset`/`end_offset`. Each answer records `tools_used`, `escalated` and `evidence_levels` in the message metadata. The UI shows **local vision** and **Gemini video** badges.

**Cost trade-off:** the agent spends 2–4 short text requests per question instead of one request carrying the whole video. That's far fewer tokens, but more *requests*, which matters on the free tier (20 requests per day per model). Set `AGENT_MODEL` to a lighter model to separate the two quotas.

# Phase 1 foundations

```
Browser ──► Next.js (same origin, /api/* proxied) ──► FastAPI
                                                        │
             ┌──────────────────────────────────────────┼─────────────────────────┐
             │ API routes (auth, video-sources, conversations, voice)             │
             │        │                          │                                │
             │  Voice adapters            ConversationOrchestrator                │
             │  SpeechToText / TextToSpeech   │  source resolution, history,      │
             │  (mock | gemini | http)        │  rolling context, persistence     │
             │        └────── text ──────────►│                                   │
             │                                ▼                                   │
             │                     VideoSessionService ──► VideoGateway           │
             │                     (prepare once, reuse)    VideoSource (url|local)│
             │                                │             SSRF-safe fetching    │
             │                                ▼                                   │
             │                     VideoAnalyzer (interface)                      │
             │                       ├── GeminiVideoAnalyzer   (now)              │
             │                       ├── MockVideoAnalyzer     (dev, no key)      │
             │                       └── Local / Hybrid        (future)           │
             └────────────────────────────────────────────────────────────────────┘
                   PostgreSQL (state, audit, events)      Redis (rate limits, cache)
```

The frontend never calls Gemini, and no key ever leaves the server.

## Where things live

| Concern | Module |
|---|---|
| VideoSource abstraction | `backend/app/video/sources/base.py` |
| URL / uploaded file / local file / live stubs | `url_source.py`, `upload_source.py`, `local_source.py`, `live.py` |
| SSRF protection | `backend/app/video/url_safety.py` |
| Video gateway (source manager, metadata, temp clips) | `backend/app/video/gateway.py` |
| VideoAnalyzer interface + normalized result | `backend/app/analyzers/base.py` |
| Gemini implementation | `backend/app/analyzers/gemini.py` |
| Analyzer selection | `backend/app/analyzers/factory.py` |
| Orchestrator | `backend/app/conversation/orchestrator.py` |
| Conversation memory | `backend/app/conversation/context.py` |
| Voice interfaces/providers | `backend/app/voice/` |
| Event vocabulary | `backend/app/events/types.py` |

## Key contracts

**`VideoSource.acquire_media(workdir, window) -> MediaHandle`**. Analyzers never touch sources. They receive a local file or a provider-fetchable URI. For URLs that means "download". For RTSP it will mean "record `window` from the ingest ring buffer into a clip". `MediaHandle.release()` deletes temp copies as soon as the analyzer is done. Footage is not retained.

**`VideoAnalyzer.prepare(media) -> PreparedVideo`** and **`analyze(prepared, query) -> AnalysisResult`**. `AnalysisResult` (answer, confidence, timestamps, events, evidence, referenced_entities, usage) is our own shape. Gemini's JSON schema is private to `gemini.py` and normalized there.

**Conversation memory** has two layers: (1) the last 8 Q/A pairs, replayed verbatim with their timestamps; (2) rolling state in `conversations.context` (entities discussed, focus times, recent events). Together these resolve "umwe muri bo" and "iyo modoka". If a user names another camera by its location ("camera yo ku irembo"), the orchestrator switches source (conservative matching).

## Cost

- The video is uploaded to Gemini **once per session** and reused until expiry (~46 h), not per question.
- The video part is first in every prompt, so repeat questions share a prefix and benefit from Gemini's implicit caching.
- `GEMINI_VIDEO_FPS` lowers frame sampling for long, static CCTV footage.
- `analysis_requests` records analyzer, model, latency and token counts per question, as the baseline for measuring savings from the local engine.

## Hybrid engine: status

Built. The local engine and the tool-calling agent are described at the top of this document. Next steps:
- Live sources: run the same pipeline continuously on a stream (`live.py`), with the event engine writing `occurred_at`.
- A local VLM as the escalation target, or as the agent LLM, to cut Gemini usage further.
- Fine-tuning the detector on footage from your own cameras once the pretrained models show gaps.

## Adding RTSP / ONVIF / NVR

The `live.py` stubs define the shape. Implement `validate` (probe stream), `acquire_media` (clip from ring buffer) and `playback` (backend re-publishes HLS/WebRTC as `type="proxy"`), add an ingest worker, and enable the kind in `VideoGateway.build`. Camera credentials go in a secret store referenced from a `cameras` table, never in `video_sources.uri`.

## Database

Tables: `users`, `video_sources` (+ `scene_config`, `recorded_start_at`), `video_sessions`, `conversations`, `conversation_messages`, `analysis_requests`, `video_events` (+ `evidence_level`, `object_class`, `track_id`, `zone`, `vision_run_id`), `vision_runs`, `object_tracks`, `scene_snapshots`. Migrations are in `backend/alembic/versions` (0001 initial, 0002 local vision).

Planned tables, deliberately not created yet:
- `cameras`: `video_sources.camera_id` will reference it.
- `camera_zones`: polygons for restricted-zone and entrance events.
- `camera_events`: may simply be `video_events` with `occurred_at` set.
- `video_clips`: clips cut around events.

## Security

- Auth: bcrypt passwords and a JWT in an httpOnly SameSite=Lax cookie. Bearer tokens are also accepted for API clients. All resources are owner-scoped, and a 404 is returned for other users' IDs.
- Video URLs: https only (http opt-in), no credentials in the URL, standard ports only, optional domain allowlist. **Every resolved IP must be public, and this is enforced at connect time** by a custom network backend that connects to the validated IP, which blocks DNS rebinding. Redirects are re-validated per hop, env proxies are ignored, and downloads are size-capped. Content is sniffed so HTML is rejected.
- Uploads: the file content is checked to confirm it's really a video (HTML and scripts are rejected). The size is capped at `VIDEO_MAX_UPLOAD_MB`, and oversized requests are refused by middleware before the body is read. Files are stored under `UPLOAD_DIR/<user_id>/` with server-generated names and deleted together with their source. The upload kind can't be registered through the JSON endpoint, so no user-supplied paths are accepted. The Next.js dev proxy holds request bodies in memory, so in production route `/api/video-sources/upload` straight to the backend or to object storage.
- Link imports (`video/ingest.py`): the link's host must resolve to public addresses, or to a network listed in `CAMERA_PRIVATE_NETWORKS`. Cameras on the LAN are reachable only when explicitly allowlisted, and localhost, cloud metadata and other private ranges stay blocked. yt-dlp and ffmpeg do their own networking after that check, so a hostile page could still redirect them. That residual risk is bounded by size/time limits, an ffmpeg protocol whitelist (no `file:`), and ignoring yt-dlp config files and proxies. Stream credentials (`rtsp://user:pass@…`) live only in memory during the capture: the stored URL is stripped and logs are redacted.
- Stored videos are always H.264 MP4 at a **constant frame rate**. Variable-rate camera footage is re-encoded, because local analysis times frames by index while browsers play by timestamp, and a mismatch would make detection boxes drift.
- Local files are dev-only, restricted to `LOCAL_VIDEO_DIR` with no traversal, and refused in production.
- Log redaction filter covers API keys, bearer tokens and signed URL parameters. Raw provider responses are not stored unless `STORE_RAW_PROVIDER_RESPONSES=true`.
- Rate limiting per user on questions (Redis).
- The Gemini prompt forbids identifying people or inferring sensitive traits.
