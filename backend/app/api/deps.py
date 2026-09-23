from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzers.base import VideoAnalyzer
from app.analyzers.factory import get_video_analyzer
from app.core.cache import get_cache
from app.core.config import get_settings
from app.core.errors import NotFoundError, RateLimited
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.db.session import get_db
from app.models import Conversation, User, VideoSourceRecord
from app.video.gateway import VideoGateway

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def current_user(request: Request, db: DbSession) -> User:
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    user_id = decode_access_token(token) if token else None
    user = await db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def gateway() -> VideoGateway:
    return VideoGateway(get_settings())


Gateway = Annotated[VideoGateway, Depends(gateway)]
Analyzer = Annotated[VideoAnalyzer, Depends(get_video_analyzer)]


async def owned_source(source_id: uuid.UUID, user: CurrentUser, db: DbSession) -> VideoSourceRecord:
    source = await db.get(VideoSourceRecord, source_id)
    if source is None or source.owner_id != user.id:  # same error either way: no existence leaks
        raise NotFoundError("Video source not found")
    return source


async def owned_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise NotFoundError("Conversation not found")
    return conversation


async def ask_rate_limit(user: CurrentUser) -> None:
    limit = get_settings().ask_rate_limit_per_minute
    window = int(datetime.now(UTC).timestamp() // 60)
    count = await get_cache().incr_window(f"rl:ask:{user.id}:{window}", 60)
    if count > limit:
        raise RateLimited(f"Too many questions. The limit is {limit} per minute.")
