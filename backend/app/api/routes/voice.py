from __future__ import annotations

import base64
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from app.api.deps import CurrentUser
from app.core.errors import ProviderUnavailable, ValidationFailed
from app.schemas.api import AudioOut, SynthesizeIn, TranscriptOut
from app.voice.base import ALLOWED_AUDIO_TYPES, MAX_AUDIO_BYTES
from app.voice.factory import get_stt, get_tts

router = APIRouter(prefix="/voice", tags=["voice"])


async def read_audio(upload: UploadFile) -> tuple[bytes, str]:
    mime = (upload.content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_AUDIO_TYPES:
        raise ValidationFailed(f"Unsupported audio type: {mime or 'unknown'}")
    data = await upload.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise ValidationFailed("Audio recording is too long")
    if not data:
        raise ValidationFailed("Empty audio recording")
    return data, mime


@router.post("/transcribe", response_model=TranscriptOut)
async def transcribe(user: CurrentUser, audio: Annotated[UploadFile, File()], language: Annotated[str, Form()] = "rw") -> TranscriptOut:
    data, mime = await read_audio(audio)
    stt = get_stt()
    transcript = await stt.transcribe(data, mime, language)
    return TranscriptOut(**transcript.model_dump(), is_placeholder=stt.is_placeholder)


@router.post("/synthesize", response_model=AudioOut)
async def synthesize(body: SynthesizeIn, user: CurrentUser) -> AudioOut:
    speech = await get_tts().synthesize(body.text, body.language)
    if speech is None:
        raise ProviderUnavailable("Kinyarwanda text-to-speech is not configured yet", code="tts_not_configured")
    return AudioOut(mime_type=speech.mime_type, base64=base64.b64encode(speech.audio).decode(), provider=speech.provider)
