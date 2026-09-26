"""ORM models.

Phase 1 tables: users, video_sources, video_sessions, conversations,
conversation_messages, analysis_requests, video_events.

Camera platform (docs/CAMERA_PLATFORM.md): cameras are video_sources rows with a live kind, plus
gateways, camera_credentials, camera_shares, stream_sessions and audit_logs.
"""

from app.models.analysis import AnalysisRequest
from app.models.camera import AuditLog, CameraCredential, CameraShare, Gateway, StreamSession
from app.models.conversation import Conversation, ConversationMessage
from app.models.user import User
from app.models.video import VideoEvent, VideoSession, VideoSourceRecord
from app.models.vision import ObjectTrack, SceneSnapshot, VisionRun

__all__ = [
    "AuditLog",
    "CameraCredential",
    "CameraShare",
    "Gateway",
    "StreamSession",
    "AnalysisRequest",
    "Conversation",
    "ConversationMessage",
    "User",
    "VideoEvent",
    "VideoSession",
    "VideoSourceRecord",
    "VisionRun",
    "ObjectTrack",
    "SceneSnapshot",
]
