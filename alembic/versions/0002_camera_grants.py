"""Add camera_grants: trusted camera-to-tenant assignments (F-1-S5).

A static camera has an optional physical site and explicit grants to one or
more tenants. Current rows govern historical event access per read (new
grants expose earlier history, revocation removes access).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "camera_grants",
        sa.Column("camera_id", sa.Text(), primary_key=True),
        # Optional physical site; NULL means the camera has no site.
        sa.Column("site_id", sa.Text(), nullable=True),
        sa.Column(
            "tenant_ids",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # One camera row per site (or NULL) is enforced by the PK; this index
    # supports site-scoped inventory queries.
    op.create_index(
        "idx_camera_grants_site",
        "camera_grants",
        ["site_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_camera_grants_site", table_name="camera_grants")
    op.drop_table("camera_grants")
