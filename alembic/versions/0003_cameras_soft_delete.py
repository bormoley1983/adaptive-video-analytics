"""Add cameras: trusted static-camera registry with soft-delete (F-1-S6).

A camera is a first-class entity distinct from its tenant grants. The
``cameras`` row carries the optional physical site and a nullable
``deleted_at`` marker. Soft-deleting a camera hides it from inventory while
retaining the row so that events referencing ``camera_id`` keep a resolvable
target (event-to-camera mapping). Grants remain independently revocable via
hard delete on ``camera_grants``; they are safe to remove because the camera
row, not the grant row, is what events map to.

Revision 0003 was reserved for an earlier (reverted) attempt that added
soft-delete to ``camera_grants`` directly; it was never applied anywhere, so
this migration chains from 0002 and reuses the 0003 slot.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cameras",
        sa.Column("camera_id", sa.Text(), primary_key=True),
        # Optional physical site; NULL means the camera has no site.
        sa.Column("site_id", sa.Text(), nullable=True),
        # Soft-delete marker: set when the camera is deleted, NULL while active.
        sa.Column(
            "deleted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Supports inventory queries that filter active cameras (deleted_at IS NULL).
    op.create_index(
        "idx_cameras_deleted_at",
        "cameras",
        ["deleted_at"],
    )
    # Backfill: every camera referenced by any existing record becomes an
    # active camera row. Two sources are merged:
    #   1. camera_grants — provides the canonical site_id assignment.
    #   2. events         — covers cameras with historical events but no
    #      current grant (e.g. after revocation). These get site_id NULL
    #      because no grant exists to supply a site; this preserves the
    #      event-to-camera mapping without inventing site assignments.
    # ON CONFLICT DO NOTHING ensures grant-sourced rows (with site_id) win
    # over event-sourced rows (site_id NULL) when both reference the same
    # camera — grants are inserted first.
    op.execute(
        """
        INSERT INTO cameras (camera_id, site_id, updated_at)
        SELECT camera_id, site_id, now()
        FROM camera_grants
        ON CONFLICT (camera_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO cameras (camera_id, site_id, updated_at)
        SELECT DISTINCT camera_id, NULL, now()
        FROM events
        ON CONFLICT (camera_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("idx_cameras_deleted_at", table_name="cameras")
    op.drop_table("cameras")
