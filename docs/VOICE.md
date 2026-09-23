# Voice (Kinyarwanda STT / TTS)

Voice is an adapter around the text conversation:

```
🎙️ audio ──► SpeechToText ──► text ──► ConversationOrchestrator ──► answer text ──► TextToSpeech ──► 🔊
```

`POST /api/conversations/{id}/voice` (multipart `audio`, `speak`) does all of it in one round trip and returns the transcript, both messages, and base64 audio when TTS is available.

## Providers

| Setting | Value | What it does |
|---|---|---|
| `STT_PROVIDER` | `mock` (default) | Returns the fixed sample question "Ni iki kiri kuba kuri iyi video?". Flagged `is_placeholder` so the UI says so. |
| | `gemini` | Gemini audio understanding using `GEMINI_API_KEY`. A reasonable first real option for Kinyarwanda. |
| | `http` | Your own model server (below). |
| `TTS_PROVIDER` | `mock` (default) | No audio. The UI shows that voice replies aren't configured. |
| | `http` | Your own model server (below). |

## HTTP contract for self-hosted models

For example a Kinyarwanda Whisper/MMS fine-tune for STT, and an MMS-TTS / Coqui voice for TTS.

**STT**: `POST {STT_HTTP_URL}`, multipart form: `audio` (file), `language` (`rw`), and header `Authorization: Bearer {STT_HTTP_API_KEY}` if set.
Response: `{"text": "...", "confidence": 0.93, "language": "rw"}`

**TTS**: `POST {TTS_HTTP_URL}`, JSON `{"text": "...", "language": "rw", "voice": TTS_VOICE}`.
Response: raw audio bytes with an `audio/*` content type (e.g. `audio/wav`, `audio/mpeg`).

To add a vendor SDK instead, implement `SpeechToText` / `TextToSpeech` in `backend/app/voice/providers.py` and register it in `voice/factory.py`.
