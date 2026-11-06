"""PostgreSQL adapter for the trusted static-camera registry (F-1-S6).

Implements the provider-independent :class:`CameraRepository` port over the
``cameras`` table (alembic revision 0003) and provides the admin write path
used by the camera delete endpoint. Reads return the *current* state per
camera so policy is re-evaluated on every request; no caching assumptions.

Soft-delete: ``delete_camera`` sets ``deleted_at`` on the row instead of
removing it. Deleted rows are invisible to :meth:`get_camera` and
:meth:`list_cameras` but retained so that events referencing ``camera_id``
keep a resolvable target (event-to-camera mapping). Only
:meth:`find_camera_by_camera_id` returns soft-deleted rows (for admin
inspection via ``GET /cameras/{camera_id}``).

Grants are independently revocable via hard delete on
:class:`PostgresGrantRepository`; they are safe to remove because the camera
row, not the grant row, is what events map to.
"""

from __future__ import annotations

import logging

from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool

from ..application.access import Camera

logger = logging.getLogger(__name__)

_SELECT_CAMERA_SQL = """
SELECT camera_id, site_id, deleted_at, updated_at
FROM cameras
WHERE camera_id = %s AND deleted_at IS NULL
"""

_SELECT_ALL_CAMERAS_SQL = """
SELECT camera_id, site_id, deleted_at, updated_at
FROM cameras
WHERE deleted_at IS NULL
ORDER BY camera_id
"""

_SELECT_CAMERA_INCLUDE_DELETED_SQL = """
SELECT camera_id, site_id, deleted_at, updated_at
FROM cameras
WHERE camera_id = %s
"""

_UPSERT_CAMERA_SQL = """
INSERT INTO cameras (camera_id, site_id, deleted_at, updated_at)
VALUES (%s, %s, NULL, now())
ON CONFLICT (camera_id) DO UPDATE
SET site_id = EXCLUDED.site_id,
    deleted_at = NULL,
    updated_at = now()
"""

_SOFT_DELETE_CAMERA_SQL = """
UPDATE cameras SET deleted_at = now(), updated_at = now()
WHERE camera_id = %s AND deleted_at IS NULL
"""


def _row_to_camera(row: tuple) -> Camera:
    """Convert a (camera_id, site_id, deleted_at, updated_at) row to a Camera."""
    camera_id, site_id, deleted_at, updated_at = row[0], row[1], row[2], row[3]
    if not isinstance(camera_id, str) or not camera_id:
        raise ValueError(f"missing camera_id in row: {row!r}")
    if site_id is not None and (not isinstance(site_id, str) or not site_id):
        raise ValueError(f"malformed site_id for {camera_id!r}: {site_id!r}")
    deleted_iso = deleted_at.isoformat() if hasattr(deleted_at, "isoformat") else None
    updated_iso = updated_at.isoformat() if hasattr(updated_at, "isoformat") else None
    return Camera(
        camera_id=camera_id,
        site_id=site_id,
        deleted_at=deleted_iso,
        updated_at=updated_iso,
    )


class PostgresCameraRepository:
    """CameraRepository over PostgreSQL; also exposes the admin write path."""

    EXPECTED_REVISION = "0003"

    def __init__(self, dsn: str, *, pool_min: int = 1, pool_max: int = 4) -> None:
        self._dsn = dsn
        pool = ConnectionPool(
            conninfo=dsn, min_size=pool_min, max_size=pool_max, name="ava-cameras"
        )
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            pool.close()
            raise
        self._pool = pool

    def check_schema(self) -> None:
        """Verify the cameras table exists (revision >= 0003)."""
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
                f"camera repository expects {self.EXPECTED_REVISION!r}. "
                "Run 'alembic upgrade head' to apply pending migrations."
            )

    def get_camera(self, camera_id: str) -> Camera | None:
        """Current active state for *camera_id*, or None (absent or deleted)."""
        with self._pool.connection() as conn:
            row = conn.execute(_SELECT_CAMERA_SQL, (camera_id,)).fetchone()
        if row is None:
            return None
        return _row_to_camera(row)

    def list_cameras(self) -> list[Camera]:
        """All active cameras; soft-deleted rows are excluded."""
        with self._pool.connection() as conn:
            rows = conn.execute(_SELECT_ALL_CAMERAS_SQL).fetchall()
        cameras: list[Camera] = []
        for row in rows:
            try:
                cameras.append(_row_to_camera(row))
            except ValueError as exc:
                logger.warning("skipping unusable camera row: %s", exc)
        return cameras

    def upsert_camera(self, camera_id: str, site_id: str | None) -> None:
        """Create or re-activate a camera row (admin)."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_UPSERT_CAMERA_SQL, (camera_id, site_id))

    def delete_camera(self, camera_id: str) -> bool:
        """Soft-delete the camera for *camera_id*. True if an active row existed."""
        with self._pool.connection() as conn, conn.transaction():
            cur = conn.execute(_SOFT_DELETE_CAMERA_SQL, (camera_id,))
            return cur.rowcount > 0

    def find_camera_by_camera_id(self, camera_id: str) -> Camera | None:
        """Full camera row including soft-deleted rows; None when absent."""
        with self._pool.connection() as conn:
            row = conn.execute(
                _SELECT_CAMERA_INCLUDE_DELETED_SQL, (camera_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_camera(row)

    def close(self) -> None:
        self._pool.close()
