from __future__ import annotations

import base64
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy import select

from app.api.deps import Analyzer, CurrentUser, DbSession, Gateway, ask_rate_limit, owned_conversation
from app.api.routes.video_sources import serialize_source
from app.api.routes.voice import read_audio
from app.conversation.orchestrator import ConversationOrchestrator
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.models import Conversation, ConversationMessage, VideoSourceRecord
from app.schemas.api import AskIn, AskOut, AudioOut, ConversationDetailOut, ConversationIn, ConversationOut, MessageOut, TranscriptOut
from app.voice.factory import get_stt, get_tts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conversations", tags=["conversations"])
OwnedConversation = Annotated[Conversation, Depends(owned_conversation)]


@router.get("", response_model=list[ConversationOut])
async def list_conversations(user: CurrentUser, db: DbSession, video_source_id: uuid.UUID | None = None) -> list[Conversation]:
    query = select(Conversation).where(Conversation.user_id == user.id)
    if video_source_id:
        query = query.where(Conversation.video_source_id == video_source_id)
    rows = await db.execute(query.order_by(Conversation.updated_at.desc()).limit(50))
    return list(rows.scalars().all())


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(body: ConversationIn, user: CurrentUser, db: DbSession) -> Conversation:
    if body.video_source_id:
        source = await db.get(VideoSourceRecord, body.video_source_id)
        if source is None or source.owner_id != user.id:
            raise NotFoundError("Video source not found")
    conversation = Conversation(user_id=user.id, video_source_id=body.video_source_id, language=body.language or user.preferred_language)
    db.add(conversation)
    await db.commit()
    return conversation


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(conversation: OwnedConversation, db: DbSession) -> ConversationDetailOut:
    rows = await db.execute(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id).order_by(ConversationMessage.created_at))
    return ConversationDetailOut(
        **ConversationOut.model_validate(conversation).model_dump(),
        messages=[MessageOut.model_validate(m) for m in rows.scalars().all()],
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation: OwnedConversation, db: DbSession) -> None:
    await db.delete(conversation)
    await db.commit()


async def _speak(text: str, language: str) -> AudioOut | None:
    try:
        speech = await get_tts().synthesize(text, language)
    except AppError as exc:
        logger.info("TTS unavailable: %s", exc.code)
        return None
    if speech is None:
        return None
    return AudioOut(mime_type=speech.mime_type, base64=base64.b64encode(speech.audio).decode(), provider=speech.provider)


async def _turn(conversation: Conversation, question: str, input_mode: str, speak: bool, db: DbSession, gw: Gateway, analyzer: Analyzer, transcript: TranscriptOut | None = None) -> AskOut:
    result = await ConversationOrchestrator(db, gw, analyzer).ask(conversation=conversation, question=question, input_mode=input_mode)
    tts = get_tts()
    audio = await _speak(result.assistant_message.content, conversation.language) if speak else None
    return AskOut(
        user_message=MessageOut.model_validate(result.user_message),
        assistant_message=MessageOut.model_validate(result.assistant_message),
        switched_to_source=serialize_source(result.switched_to_source, gw) if result.switched_to_source else None,
        transcript=transcript,
        audio=audio,
        tts_available=not tts.is_placeholder,
    )


@router.post("/{conversation_id}/messages", response_model=AskOut, dependencies=[Depends(ask_rate_limit)])
async def ask_text(body: AskIn, conversation: OwnedConversation, db: DbSession, gw: Gateway, analyzer: Analyzer) -> AskOut:
    return await _turn(conversation, body.question, "text", body.speak, db, gw, analyzer)


@router.post("/{conversation_id}/voice", response_model=AskOut, dependencies=[Depends(ask_rate_limit)])
async def ask_voice(
    conversation: OwnedConversation,
    db: DbSession,
    gw: Gateway,
    analyzer: Analyzer,
    audio: Annotated[UploadFile, File()],
    speak: Annotated[bool, Form()] = True,
) -> AskOut:
    """Speak -> STT -> orchestrator -> TTS, in one round trip."""
    data, mime = await read_audio(audio)
    stt = get_stt()
    transcript = await stt.transcribe(data, mime, conversation.language or get_settings().default_language)
    transcript_out = TranscriptOut(**transcript.model_dump(), is_placeholder=stt.is_placeholder)
    if not transcript.text:
        raise AppError("No speech was recognised. Please try again.", code="empty_transcript")
    return await _turn(conversation, transcript.text, "voice", speak, db, gw, analyzer, transcript_out)
