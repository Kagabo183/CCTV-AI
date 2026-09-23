"""Initial Phase 1 schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("preferred_language", sa.String(8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "video_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("source_metadata", JSONB, nullable=False),
        sa.Column("last_validated_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name="fk_video_sources_owner_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_video_sources"),
    )
    op.create_index("ix_video_sources_owner_id", "video_sources", ["owner_id"])

    op.create_table(
        "video_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("analyzer", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("provider_ref", JSONB, nullable=False),
        sa.Column("provider_ref_expires_at", TS, nullable=True),
        sa.Column("media_metadata", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_video_sessions_video_source_id_video_sources", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_video_sessions_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_video_sessions"),
    )
    op.create_index("ix_video_sessions_video_source_id", "video_sessions", ["video_source_id"])
    op.create_index("ix_video_sessions_user_id", "video_sessions", ["user_id"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=True),
        sa.Column("video_session_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("context", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_conversations_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_conversations_video_source_id_video_sources", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["video_session_id"], ["video_sessions.id"], name="fk_conversations_video_session_id_video_sessions", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_video_source_id", "conversations", ["video_source_id"])

    op.create_table(
        "analysis_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("video_session_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("analyzer", sa.String(32), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("raw_response", JSONB, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_analysis_requests_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_analysis_requests_video_source_id_video_sources", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_session_id"], ["video_sessions.id"], name="fk_analysis_requests_video_session_id_video_sessions", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_analysis_requests_conversation_id_conversations", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_analysis_requests"),
    )
    op.create_index("ix_analysis_requests_user_id", "analysis_requests", ["user_id"])
    op.create_index("ix_analysis_requests_video_source_id", "analysis_requests", ["video_source_id"])
    op.create_index("ix_analysis_requests_created_at", "analysis_requests", ["created_at"])

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(8), nullable=True),
        sa.Column("input_mode", sa.String(8), nullable=True),
        sa.Column("video_source_id", sa.Uuid(), nullable=True),
        sa.Column("analysis_request_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("timestamps", JSONB, nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column("message_metadata", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_conversation_messages_conversation_id_conversations", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_conversation_messages_video_source_id_video_sources", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["analysis_request_id"], ["analysis_requests.id"], name="fk_conversation_messages_analysis_request_id_analysis_requests", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_messages"),
    )
    op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
    op.create_index("ix_conversation_messages_created_at", "conversation_messages", ["created_at"])

    op.create_table(
        "video_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("video_session_id", sa.Uuid(), nullable=True),
        sa.Column("analysis_request_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=True),
        sa.Column("end_time", sa.Float(), nullable=True),
        sa.Column("occurred_at", TS, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("detector", sa.String(32), nullable=False),
        sa.Column("event_metadata", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_video_events_video_source_id_video_sources", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_session_id"], ["video_sessions.id"], name="fk_video_events_video_session_id_video_sessions", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["analysis_request_id"], ["analysis_requests.id"], name="fk_video_events_analysis_request_id_analysis_requests", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_video_events"),
    )
    op.create_index("ix_video_events_video_source_id", "video_events", ["video_source_id"])
    op.create_index("ix_video_events_source_type", "video_events", ["video_source_id", "event_type"])


def downgrade() -> None:
    for table in ("video_events", "conversation_messages", "analysis_requests", "conversations", "video_sessions", "video_sources", "users"):
        op.drop_table(table)
