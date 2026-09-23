"""Public API request/response models. Nothing here exposes secrets or
provider-specific shapes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)
    preferred_language: Literal["rw", "en"] = "rw"


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class UserOut(ApiModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    preferred_language: str


class AuthOut(BaseModel):
    user: UserOut
    access_token: str
    expires_at: datetime


# --- video sources ----------------------------------------------------------


class PlaybackOut(BaseModel):
    type: Literal["direct", "youtube", "proxy"]
    url: str


class VideoSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    kind: Literal["url", "local", "rtsp", "onvif", "nvr"] = "url"
    uri: str = Field(min_length=1, max_length=2048)


class VideoSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)


class VideoSourceOut(ApiModel):
    id: uuid.UUID
    name: str
    location: str | None
    kind: str
    uri: str
    status: str
    status_message: str | None
    metadata: dict[str, Any]
    playback: PlaybackOut | None
    created_at: datetime


class VideoSessionOut(ApiModel):
    id: uuid.UUID
    video_source_id: uuid.UUID
    analyzer: str
    status: str
    status_message: str | None
    provider_ref_expires_at: datetime | None


class VideoEventOut(ApiModel):
    id: uuid.UUID
    event_type: str
    description: str
    start_time: float | None
    end_time: float | None
    confidence: float | None
    detector: str
    created_at: datetime


# --- conversations ----------------------------------------------------------


class ConversationIn(BaseModel):
    video_source_id: uuid.UUID | None = None
    language: Literal["rw", "en"] | None = None


class MessageOut(ApiModel):
    id: uuid.UUID
    role: str
    content: str
    language: str | None
    input_mode: str | None
    video_source_id: uuid.UUID | None
    confidence: float | None
    timestamps: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(validation_alias="message_metadata")
    created_at: datetime


class ConversationOut(ApiModel):
    id: uuid.UUID
    video_source_id: uuid.UUID | None
    title: str | None
    language: str
    created_at: datetime
    updated_at: datetime


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut]


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    speak: bool = False


class AudioOut(BaseModel):
    mime_type: str
    base64: str
    provider: str


class TranscriptOut(BaseModel):
    text: str
    language: str
    confidence: float | None
    provider: str
    is_placeholder: bool


class AskOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
    switched_to_source: VideoSourceOut | None = None
    transcript: TranscriptOut | None = None
    audio: AudioOut | None = None
    tts_available: bool


class SynthesizeIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    language: Literal["rw", "en"] = "rw"


# --- system -----------------------------------------------------------------


class PublicConfigOut(BaseModel):
    analyzer: str
    analyzer_is_mock: bool
    stt_provider: str
    stt_is_placeholder: bool
    tts_provider: str
    tts_is_placeholder: bool
    source_kinds: list[str]
    default_language: str
