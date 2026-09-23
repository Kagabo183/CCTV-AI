"""Local vision engine: runs, tracks, snapshots; evidence levels on events

Revision ID: 0002_local_vision
Revises: 0001_initial
Create Date: 2026-09-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_local_vision"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "vision_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detector", sa.String(32), nullable=False),
        sa.Column("weights", sa.String(100), nullable=False),
        sa.Column("tracker", sa.String(32), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("stats", JSONB, nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_vision_runs_video_source_id_video_sources", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_vision_runs"),
    )
    op.create_index("ix_vision_runs_video_source_id", "vision_runs", ["video_source_id"])

    op.create_table(
        "object_tracks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vision_run_id", sa.Uuid(), nullable=False),
        sa.Column("video_source_id", sa.Uuid(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("object_class", sa.String(32), nullable=False),
        sa.Column("first_seen", sa.Float(), nullable=False),
        sa.Column("last_seen", sa.Float(), nullable=False),
        sa.Column("frames", sa.Integer(), nullable=False),
        sa.Column("mean_confidence", sa.Float(), nullable=False),
        sa.Column("max_confidence", sa.Float(), nullable=False),
        sa.Column("track_metadata", JSONB, nullable=False),
        sa.ForeignKeyConstraint(["vision_run_id"], ["vision_runs.id"], name="fk_object_tracks_vision_run_id_vision_runs", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_source_id"], ["video_sources.id"], name="fk_object_tracks_video_source_id_video_sources", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_object_tracks"),
    )
    op.create_index("ix_object_tracks_vision_run_id", "object_tracks", ["vision_run_id"])
    op.create_index("ix_object_tracks_run_class", "object_tracks", ["vision_run_id", "object_class"])

    op.create_table(
        "scene_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vision_run_id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("counts", JSONB, nullable=False),
        sa.Column("track_ids", JSONB, nullable=False),
        sa.ForeignKeyConstraint(["vision_run_id"], ["vision_runs.id"], name="fk_scene_snapshots_vision_run_id_vision_runs", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_scene_snapshots"),
    )
    op.create_index("ix_scene_snapshots_run_time", "scene_snapshots", ["vision_run_id", "timestamp"])

    with op.batch_alter_table("video_events") as batch:
        batch.add_column(sa.Column("vision_run_id", sa.Uuid(), nullable=True))
        # Existing rows all came from Gemini answers.
        batch.add_column(sa.Column("evidence_level", sa.String(24), nullable=False, server_default="model_interpretation"))
        batch.add_column(sa.Column("object_class", sa.String(32), nullable=True))
        batch.add_column(sa.Column("track_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("zone", sa.String(100), nullable=True))
        batch.create_foreign_key("fk_video_events_vision_run_id_vision_runs", "vision_runs", ["vision_run_id"], ["id"], ondelete="CASCADE")
        batch.create_index("ix_video_events_vision_run_id", ["vision_run_id"])

    with op.batch_alter_table("video_sources") as batch:
        batch.add_column(sa.Column("scene_config", JSONB, nullable=False, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("recorded_start_at", TS, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("video_sources") as batch:
        batch.drop_column("recorded_start_at")
        batch.drop_column("scene_config")
    with op.batch_alter_table("video_events") as batch:
        batch.drop_index("ix_video_events_vision_run_id")
        batch.drop_constraint("fk_video_events_vision_run_id_vision_runs", type_="foreignkey")
        for column in ("zone", "track_id", "object_class", "evidence_level", "vision_run_id"):
            batch.drop_column(column)
    op.drop_table("scene_snapshots")
    op.drop_table("object_tracks")
    op.drop_table("vision_runs")
