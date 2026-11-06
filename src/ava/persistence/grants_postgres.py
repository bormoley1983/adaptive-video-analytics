"""PostgreSQL adapter for trusted camera grants (F-1-S5, decision D1).

Implements the provider-independent :class:`GrantRepository` port over the
``camera_grants`` table (alembic revision 0002) and provides the admin
write path used by the grant mutation endpoint. Reads return the *current*
assignment per camera so policy is re-evaluated on every request; no
caching assumptions.

Malformed rows (e.g. NULL tenant_ids despite NOT NULL, or a row that fails
type checks) raise :class:`GrantLookupError` from the lookup path and are
skipped by the collection path — access is denied, never inferred.
"""

from __future__ import annotations

import logging

from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool

from ..application.access import CameraGrant, GrantLookupError

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

    EXPECTED_REVISION = "0002"

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
        """Create or replace the trusted assignment for *camera_id* (admin)."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                _UPSERT_GRANT_SQL, (camera_id, site_id, list(tenant_ids))
            )

    def revoke_camera_grant(self, camera_id: str) -> bool:
        """Remove the assignment for *camera_id* (admin). True if a row existed."""
        with self._pool.connection() as conn, conn.transaction():
            cur = conn.execute(_DELETE_GRANT_SQL, (camera_id,))
            return cur.rowcount > 0

    def close(self) -> None:
        self._pool.close()
