# Architecture (Phase 1)

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

## Evolving toward the hybrid engine

1. Add `LocalVideoAnalyzer` (YOLO/RT-DETR + ByteTrack + event engine) implementing `VideoAnalyzer` with `capabilities.continuous_events=True`. It writes `video_events` continuously with `detector="local_engine"`.
2. Add `HybridVideoAnalyzer(local, reasoning=GeminiVideoAnalyzer)`. It answers counting and presence questions from `video_events`, and escalates visual reasoning to Gemini with only the relevant clip (`TimeWindow` → Gemini `VideoMetadata.start_offset/end_offset`).
3. Register it in `analyzers/factory.py`. The orchestrator, API and frontend don't change.

## Adding RTSP / ONVIF / NVR

The `live.py` stubs define the shape. Implement `validate` (probe stream), `acquire_media` (clip from ring buffer) and `playback` (backend re-publishes HLS/WebRTC as `type="proxy"`), add an ingest worker, and enable the kind in `VideoGateway.build`. Camera credentials go in a secret store referenced from a `cameras` table, never in `video_sources.uri`.

## Database

Phase 1 tables: `users`, `video_sources`, `video_sessions`, `conversations`, `conversation_messages`, `analysis_requests`, `video_events`. Migrations are in `backend/alembic/versions`.

Planned tables, deliberately not created yet:
- `cameras`: `video_sources.camera_id` will reference it.
- `camera_zones`: polygons for restricted-zone and entrance events.
- `camera_events`: may simply be `video_events` with `occurred_at` set.
- `video_clips`: clips cut around events.

## Security

- Auth: bcrypt passwords and a JWT in an httpOnly SameSite=Lax cookie. Bearer tokens are also accepted for API clients. All resources are owner-scoped, and a 404 is returned for other users' IDs.
- Video URLs: https only (http opt-in), no credentials in the URL, standard ports only, optional domain allowlist. **Every resolved IP must be public, and this is enforced at connect time** by a custom network backend that connects to the validated IP, which blocks DNS rebinding. Redirects are re-validated per hop, env proxies are ignored, and downloads are size-capped. Content is sniffed so HTML is rejected.
- Uploads: the file content is checked to confirm it's really a video (HTML and scripts are rejected). The size is capped at `VIDEO_MAX_UPLOAD_MB`, and oversized requests are refused by middleware before the body is read. Files are stored under `UPLOAD_DIR/<user_id>/` with server-generated names and deleted together with their source. The upload kind can't be registered through the JSON endpoint, so no user-supplied paths are accepted. The Next.js dev proxy holds request bodies in memory, so in production route `/api/video-sources/upload` straight to the backend or to object storage.
- Local files are dev-only, restricted to `LOCAL_VIDEO_DIR` with no traversal, and refused in production.
- Log redaction filter covers API keys, bearer tokens and signed URL parameters. Raw provider responses are not stored unless `STORE_RAW_PROVIDER_RESPONSES=true`.
- Rate limiting per user on questions (Redis).
- The Gemini prompt forbids identifying people or inferring sensitive traits.
