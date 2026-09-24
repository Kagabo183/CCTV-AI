# Visionary: talk to your cameras in Kinyarwanda

Phase 1: register a **video URL** or **upload a video file**, open it, and ask questions by text or voice. Answers are grounded in the video, with clickable timestamps. Gemini is the first video analyzer, behind a replaceable `VideoAnalyzer` interface.

```
frontend/  Next.js 16 web app (video player + AI camera assistant chat)
backend/   FastAPI · PostgreSQL · Redis · Gemini (via VideoAnalyzer)
docs/      ARCHITECTURE.md, VOICE.md
```

## Quick start

### With Docker
```bash
cp backend/.env.example backend/.env      # set SECRET_KEY; add GEMINI_API_KEY when available
docker compose up --build
# open http://localhost:3000
```

### Without Docker (development)
```bash
# backend
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # (bin/ on macOS/Linux)
cp .env.example .env
.venv/Scripts/alembic upgrade head
.venv/Scripts/uvicorn app.main:app --reload --port 8000

# frontend (second terminal)
cd frontend && npm install && npm run dev
```
No Postgres/Redis handy? You can run with `DATABASE_URL=sqlite+aiosqlite:///./dev.db` and `REDIS_URL=` (empty) in `backend/.env`. That's for development only.

### Tests
```bash
cd backend && .venv/Scripts/python -m pytest
```

## Adding videos: links, cameras, uploads

Press **+** and paste almost any link. It is checked, then **imported** in the background as a browser-playable H.264 MP4, so every source plays in the page with model boxes and runs through local analysis:

| Link | How it is imported |
|---|---|
| YouTube and other video sites, web pages with an embedded video | yt-dlp (needs Node.js for YouTube) |
| Direct files (MP4, MOV, WebM, MKV, AVI, TS, …) | downloaded through the SSRF-safe client |
| HLS `.m3u8` | recorded video downloaded whole; live HLS recorded as a clip |
| RTSP cameras (`rtsp://user:pass@192.168.1.64:554/…`), MJPEG streams | a clip of the chosen length is recorded with ffmpeg |
| Uploads (drag and drop) | stored; HEVC/AVI/variable-frame-rate files are converted |

- Cameras on your LAN: add their network to `CAMERA_PRIVATE_NETWORKS` (e.g. `192.168.1.0/24`). All other private addresses stay blocked. Camera passwords are used for the recording only and are never stored.
- Videos longer than `VIDEO_IMPORT_MAX_SECONDS` (default 30 min) are cut, and anything over 1080p is downscaled when re-encoded. NVIDIA GPUs encode with NVENC automatically.
- Only import videos you have the right to use. Some sites (e.g. Vimeo) now require a login and are refused with a clear message.

## Local vision engine (detection → tracking → events)

Runs on the GPU, using pretrained models downloaded on first use:
```bash
cd backend
.venv/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130   # match your CUDA; Blackwell needs cu128+
.venv/Scripts/pip install -r requirements-vision.txt
.venv/Scripts/python scripts/benchmark_vision.py      # optional: compare YOLO/RT-DETR × ByteTrack/BoT-SORT
```
Every uploaded or linked video is analysed automatically. The **Vision lab** compares detectors (COCO YOLO26s, Objects365 YOLO26s, RT-DETR), and **Objects detected** lists every class found, including `unknown` for detections the model wasn't sure about. Click an object to ask a vision-language model what it really is.

Detectors are closed-set: they only know their class list and can confidently mislabel anything else. See [docs/DETECTOR_DIAGNOSIS.md](docs/DETECTOR_DIAGNOSIS.md) for the rabbit case, and [docs/VISION_BENCHMARK.md](docs/VISION_BENCHMARK.md) for speed.

Open-ended questions ("what is that animal?") go to `VIDEO_UNDERSTANDING_PROVIDERS` (default `gemini,local_vlm`). For a local fallback, run `ollama pull qwen3-vl:8b` and set `LOCAL_VLM_MODEL=qwen3-vl:8b`.

## Turning on Gemini

Add to `backend/.env` and restart the backend:
```
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.6-flash    # or whichever model you choose
```
With `VIDEO_ANALYZER_PROVIDER=auto` (default), the backend uses Gemini when a key is set and the **mock analyzer** otherwise. Mock answers are clearly labelled `[IGERAGEZA]` in the UI. Production refuses to start with the mock analyzer.

Voice: `STT_PROVIDER=gemini` gives Kinyarwanda speech recognition with the same key. For self-hosted Kinyarwanda STT/TTS models, see [docs/VOICE.md](docs/VOICE.md).

## Demo flow

1. Sign up at http://localhost:3000.
2. **+** → paste a video URL (direct MP4/WebM/MOV, or YouTube) **or** switch to *Upload file* and drop a recording (up to 500 MB). Give it a name and a location such as `irembo`.
3. The video opens and gets prepared for analysis (uploaded to Gemini once and reused).
4. Ask: *"Ni iki kiri kuba kuri iyi video?"*, then follow up: *"Umwe muri bo yinjiye?"*
5. Click a timestamp chip to jump the player to that moment.
6. Press the microphone to speak.
