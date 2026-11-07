"""Persist current camera grants and atomically reactivate their cameras.

Malformed trusted grants deny access; grant reads do not infer assignments.
"""

from __future__ import annotations

import logging

from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool

from ..application.access import CameraGrant, GrantLookupError
from .mutation_postgres import lock_camera

logger = logging.getLogger(__name__)

_SELECT_GRANT_SQL = """
SELECT camera_id, site_id, tenant_ids, updated_at
FROM camera_grants
WHERE camera_id = %s
"""

_SELECT_ALL_GRANTS_SQL = """
SELECT camera_id, site_id, tenant_ids, updated_at
FROM camera_grants
ORDER BY camera_id
"""

_UPSERT_GRANT_SQL = """
INSERT INTO camera_grants (camera_id, site_id, tenant_ids, updated_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (camera_id) DO UPDATE
SET site_id = EXCLUDED.site_id,
    tenant_ids = EXCLUDED.tenant_ids,
    updated_at = now()
"""

_DELETE_GRANT_SQL = "DELETE FROM camera_grants WHERE camera_id = %s"


def _row_to_grant(row: tuple) -> CameraGrant:
    """Convert a (camera_id, site_id, tenant_ids, updated_at) row to a grant.

    Raises :class:`GrantLookupError` on unusable data — access is denied,
    never inferred.
    """
    camera_id, site_id, tenant_ids = row[0], row[1], row[2]
    if not isinstance(camera_id, str) or not camera_id:
        raise GrantLookupError(str(camera_id), "missing camera_id")
    if site_id is not None and (not isinstance(site_id, str) or not site_id):
        raise GrantLookupError(camera_id, "malformed site_id")
    if tenant_ids is None:
        # NOT NULL column; NULL means the row is unusable — deny.
        raise GrantLookupError(camera_id, "missing tenant_ids")
    if not all(isinstance(t, str) and t for t in tenant_ids):
        raise GrantLookupError(camera_id, "malformed tenant_ids entry")
    updated_at = row[3].isoformat() if hasattr(row[3], "isoformat") else None
    return CameraGrant(
        camera_id=camera_id,
        site_id=site_id,
        tenant_ids=frozenset(tenant_ids),
        updated_at=updated_at,
    )


class PostgresGrantRepository:
    """GrantRepository over PostgreSQL; also exposes the admin write path."""

    EXPECTED_REVISION = "0004"

    def __init__(self, dsn: str, *, pool_min: int = 1, pool_max: int = 4) -> None:
        self._dsn = dsn
        pool = ConnectionPool(
            conninfo=dsn, min_size=pool_min, max_size=pool_max, name="ava-grants"
        )
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            pool.close()
            raise
        self._pool = pool

    def check_schema(self) -> None:
        """Verify the camera_grants table exists (revision >= 0002)."""
        with self._pool.connection() as conn:
            try:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
            except pg_errors.UndefinedTable:
                raise RuntimeError(
                    "Database schema is not migrated. "
                    "Run 'alembic upgrade head' before starting AVA."
                ) from None
        if row is None or row[0] != self.EXPECTED_REVISION:
            current = row[0] if row else None
            raise RuntimeError(
                f"Database schema is at revision {current!r} but the "
                f"grant repository expects {self.EXPECTED_REVISION!r}. "
                "Run 'alembic upgrade head' to apply pending migrations."
            )

    def get_camera_grant(self, camera_id: str) -> CameraGrant | None:
        """Current trusted assignment for *camera_id*, or None (deny)."""
        with self._pool.connection() as conn:
            row = conn.execute(_SELECT_GRANT_SQL, (camera_id,)).fetchone()
        if row is None:
            return None
        return _row_to_grant(row)

    def get_camera_grant_with_revision(self, camera_id: str):
        """Read a grant representation and its revision in one snapshot."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT g.camera_id, g.site_id, g.tenant_ids, g.updated_at, c.revision "
                "FROM camera_grants g JOIN cameras c USING (camera_id) WHERE camera_id = %s",
                (camera_id,),
            ).fetchone()
        return (None, None) if row is None else (_row_to_grant(row[:4]), row[4])

    def list_camera_grants(self) -> list[CameraGrant]:
        """All current trusted assignments; malformed rows are skipped."""
        with self._pool.connection() as conn:
            rows = conn.execute(_SELECT_ALL_GRANTS_SQL).fetchall()
        grants: list[CameraGrant] = []
        for row in rows:
            try:
                grants.append(_row_to_grant(row))
            except GrantLookupError as exc:
                logger.warning("skipping unusable grant row: %s", exc)
        return grants

    def set_camera_grant(
        self, camera_id: str, site_id: str | None, tenant_ids: list[str]
    ) -> None:
        """Replace the assignment and atomically create or reactivate its camera."""
        self.set_camera_grant_and_reactivate_camera(camera_id, site_id, tenant_ids)

    def revoke_camera_grant(self, camera_id: str) -> bool:
        """Remove the assignment for *camera_id* (admin). True if a row existed.

        Also increments the camera revision when the camera row exists
        .
        """
        _INCREMENT_REVISION_SQL = (
            "UPDATE cameras SET revision = revision + 1, updated_at = now() "
            "WHERE camera_id = %s"
        )
        with self._pool.connection() as conn, conn.transaction():
            lock_camera(conn, camera_id)
            cur = conn.execute(_DELETE_GRANT_SQL, (camera_id,))
            existed = cur.rowcount > 0
            if existed:
                # Advance the camera revision to signal state change.
                conn.execute(_INCREMENT_REVISION_SQL, (camera_id,))
            return existed

    def get_camera_revision(self, camera_id: str) -> int | None:
        """Return the current camera revision, or None if no row exists."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT revision FROM cameras WHERE camera_id = %s", (camera_id,)
            ).fetchone()
        return row[0] if row is not None else None

    def increment_camera_revision(self, camera_id: str) -> int:
        """Atomically increment and return the new camera revision."""
        with self._pool.connection() as conn, conn.transaction():
            lock_camera(conn, camera_id)
            cur = conn.execute(
                "UPDATE cameras SET revision = revision + 1, updated_at = now() "
                "WHERE camera_id = %s",
                (camera_id,),
            )
            if cur.rowcount == 0:
                raise ValueError(f"camera {camera_id!r} does not exist")
            row = conn.execute(
                "SELECT revision FROM cameras WHERE camera_id = %s", (camera_id,)
            ).fetchone()
            return row[0]

    def set_camera_grant_and_reactivate_camera(
        self, camera_id: str, site_id: str | None, tenant_ids: list[str]
    ) -> None:
        """Atomically upsert the grant AND re-activate the camera row.

        when an admin replaces a grant for a soft-deleted
        camera, both the grant write and the camera re-activation must commit
        or roll back together. This method uses a single connection/transaction
        spanning both tables so a partial failure cannot leave the camera
        active while the old grant still applies (or vice versa).

        If no ``cameras`` row exists for *camera_id* (e.g. the registry was
        not backfilled), the upsert creates one — consistent with the
        migration's backfill policy.
        """
        _UPSERT_CAMERA_SQL = """
        INSERT INTO cameras (camera_id, site_id, deleted_at, updated_at)
        VALUES (%s, %s, NULL, now())
        ON CONFLICT (camera_id) DO UPDATE
        SET site_id = EXCLUDED.site_id,
            deleted_at = NULL,
            updated_at = now(),
            revision = cameras.revision + 1
        """
        with self._pool.connection() as conn, conn.transaction():
            lock_camera(conn, camera_id)
            conn.execute(
                _UPSERT_GRANT_SQL, (camera_id, site_id, list(tenant_ids))
            )
            conn.execute(_UPSERT_CAMERA_SQL, (camera_id, site_id))

    def close(self) -> None:
        self._pool.close()
