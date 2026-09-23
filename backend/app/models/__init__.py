"""ORM models.

Phase 1 tables: users, video_sources, video_sessions, conversations,
conversation_messages, analysis_requests, video_events.

Planned (not created yet, see docs/ARCHITECTURE.md#database):
  cameras       - a physical camera; video_sources.camera_id will reference it
  camera_zones  - polygons per camera (restricted areas, entrances)
  camera_events - absolute-time events from the local engine (video_events
                  already carries occurred_at for this)
  video_clips   - recorded clips extracted from live streams around events
"""

from app.models.analysis import AnalysisRequest
from app.models.conversation import Conversation, ConversationMessage
from app.models.user import User
from app.models.video import VideoEvent, VideoSession, VideoSourceRecord

__all__ = [
    "AnalysisRequest",
    "Conversation",
    "ConversationMessage",
    "User",
    "VideoEvent",
    "VideoSession",
    "VideoSourceRecord",
]
