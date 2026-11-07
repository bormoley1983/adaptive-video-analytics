"""Idempotency keys table and cameras.revision column.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07

Adds the idempotency_keys table for storing mutation results keyed
by (tenant, subject, key), and a monotonic revision counter on cameras for
optimistic concurrency via ETag/If-Match.

Pending records carry no response fields; completed records require a
successful 2xx response status. A 204 may have a null body.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table(
        "idempotency_keys",
        sa.Column("key_hash", sa.String(length=64), primary_key=True),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        # Response fields are absent while pending; required when completed.
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Constrain status to pending or completed.
    op.create_check_constraint(
        "ck_idempotency_keys_status",
        "idempotency_keys",
        "status IN ('pending', 'completed')",
    )
    # Completed records must carry a successful 2xx response status.
    # Pending records have no response fields.
    op.create_check_constraint(
        "ck_idempotency_keys_response",
        "idempotency_keys",
        "(status = 'pending' AND response_status IS NULL AND response_body IS NULL AND etag IS NULL) OR (status = 'completed' AND response_status IS NOT NULL AND response_status >= 200 AND response_status < 300)",
    )
    # Index for expiration cleanup (DELETE WHERE expires_at < now).
    op.create_index(
        "ix_idempotency_keys_expires_at",
        "idempotency_keys",
        ["expires_at"],
    )


    # Monotonic per camera_id; incremented on every state-changing mutation
    # (grant PUT, grant DELETE when a row existed, camera first soft-delete).
    op.add_column(
        "cameras",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("cameras", "revision")
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
