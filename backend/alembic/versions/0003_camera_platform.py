"""Camera platform: gateways, encrypted camera credentials, sharing, stream sessions, audit log;
camera columns on video_sources (NVR parent, gateway, connection state, AI profile).

Revision ID: 0003_camera_platform
Revises: 0002_local_vision
Create Date: 2026-09-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_camera_platform"
down_revision: str | None = "0002_local_vision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "gateways",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("enrollment_token_hash", sa.String(128), nullable=True),
        sa.Column("enrollment_expires_at", TS, nullable=True),
        sa.Column("secret_hash", sa.String(128), nullable=True),
        sa.Column("last_heartbeat_at", TS, nullable=True),
        sa.Column("version", sa.String(40), nullable=True),
        sa.Column("gateway_metadata", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name="fk_gateways_owner_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_gateways"),
    )
    op.create_index("ix_gateways_owner_id", "gateways", ["owner_id"])

    with op.batch_alter_table("video_sources") as batch:
        batch.add_column(sa.Column("parent_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("gateway_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("connection_state", sa.String(20), nullable=True))
        batch.add_column(sa.Column("last_seen_at", TS, nullable=True))
        batch.add_column(sa.Column("ai_profile", JSONB, nullable=False, server_default="{}"))
        batch.create_foreign_key("fk_video_sources_parent_id_video_sources", "video_sources", ["parent_id"], ["id"], ondelete="CASCADE")
        batch.create_foreign_key("fk_video_sources_gateway_id_gateways", "gateways", ["gateway_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_video_sources_parent_id", ["parent_id"])
        batch.create_index("ix_video_sources_gateway_id", ["gateway_id"])

    op.create_table(
        "camera_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_camera_credentials_video_source_id_video_sources", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_camera_credentials"),
    )
    op.create_index("ix_camera_credentials_video_source_id", "camera_credentials", ["video_source_id"], unique=True)

    op.create_table(
        "camera_shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("permissions", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_camera_shares_video_source_id_video_sources", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_camera_shares_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_camera_shares"),
        sa.UniqueConstraint("video_source_id", "user_id", name="uq_camera_shares_source_user"),
    )
    op.create_index("ix_camera_shares_video_source_id", "camera_shares", ["video_source_id"])
    op.create_index("ix_camera_shares_user_id", "camera_shares", ["user_id"])

    op.create_table(
        "stream_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("transport", sa.String(16), nullable=False),
        sa.Column("relayed", sa.Boolean(), nullable=False),
        sa.Column("stream", sa.String(16), nullable=True),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("ended_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_stream_sessions_video_source_id_video_sources", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_stream_sessions_user_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_stream_sessions"),
    )
    op.create_index("ix_stream_sessions_video_source_id", "stream_sessions", ["video_source_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("target_type", sa.String(24), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_audit_logs_user_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("stream_sessions")
    op.drop_table("camera_shares")
    op.drop_table("camera_credentials")
    with op.batch_alter_table("video_sources") as batch:
        batch.drop_index("ix_video_sources_gateway_id")
        batch.drop_index("ix_video_sources_parent_id")
        batch.drop_constraint("fk_video_sources_gateway_id_gateways", type_="foreignkey")
        batch.drop_constraint("fk_video_sources_parent_id_video_sources", type_="foreignkey")
        for col in ("ai_profile", "last_seen_at", "connection_state", "gateway_id", "parent_id"):
            batch.drop_column(col)
    op.drop_table("gateways")
