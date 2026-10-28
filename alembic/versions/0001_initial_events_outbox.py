"""Initial migration: events and outbox tables."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("camera_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False, server_default=sa.text("-1")),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("rule_type", sa.Text(), nullable=False),
        sa.Column("class_label", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(precision=53), nullable=False),
        sa.Column("source_time_s", sa.Float(precision=53), nullable=False),
        sa.Column("emitted_utc", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("capture_utc", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("time_basis", sa.Text(), nullable=False, server_default=sa.text("'file_pts_only'")),
        sa.Column("config_version", sa.Text(), nullable=False),
        sa.Column("model_ref", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("frame_index", sa.Integer(), nullable=False, server_default=sa.text("-1")),
        sa.Column("evidence_kind", sa.Text(), nullable=True),
        sa.Column("evidence_path", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_index(
        "idx_events_camera_time",
        "events",
        ["camera_id", sa.text("source_time_s DESC")],
    )
    op.create_index(
        "idx_events_session",
        "events",
        ["session_id"],
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"]),
        sa.Column("aggregate_type", sa.Text(), nullable=False, server_default=sa.text("'event'")),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_index(
        "idx_outbox_unpublished",
        "outbox",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_outbox_unpublished", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("idx_events_session", table_name="events")
    op.drop_index("idx_events_camera_time", table_name="events")
    op.drop_table("events")
