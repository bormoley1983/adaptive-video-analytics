"""PostgreSQL event sink adapter (ADR-16).

Implements :class:`ava.persistence.ports.EventSink` against PostgreSQL 18
using ``psycopg`` v3 and a ``psycopg_pool.ConnectionPool``. No ORM.

The transaction boundary is internal to :meth:`write`: each call opens a
transaction, inserts the event row and its outbox row atomically, and
commits. Duplicate ``event_id`` delivery is idempotent via
``ON CONFLICT DO NOTHING``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool

from ..events import EventRecord

logger = logging.getLogger(__name__)

_INSERT_EVENT_SQL = """
INSERT INTO events (
    event_id, camera_id, session_id, track_id, rule_id, rule_type,
    class_label, confidence, source_time_s, emitted_utc, capture_utc,
    time_basis, config_version, model_ref, schema_version, frame_index,
    evidence_kind, evidence_path, payload
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s::jsonb
)
ON CONFLICT (event_id) DO NOTHING
"""

_INSERT_OUTBOX_SQL = """
INSERT INTO outbox (event_id, aggregate_type, payload)
VALUES (%s, 'event', %s::jsonb)
"""


def _parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC string to a ``datetime`` for PostgreSQL."""
    if value is None:
        return None
    # EventRecord stores emitted_utc as "2026-10-27T12:00:00.000Z"
    return datetime.fromisoformat(value)


class PostgresEventSink:
    """PostgreSQL event sink using psycopg v3 and a connection pool."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 1,
        pool_max: int = 4,
    ) -> None:
        self._dsn = dsn
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=pool_min,
            max_size=pool_max,
            name="ava-events",
        )
        try:
            # Verify the pool is usable immediately (fail fast on bad DSN).
            with pool.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            pool.close()
            raise
        self._pool = pool

    EXPECTED_REVISION = "0001"

    def check_connection(self) -> None:
        """Verify a database connection is available."""
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")

    def check_schema(self) -> None:
        """Verify the schema is migrated to EXPECTED_REVISION."""
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

        if row is None:
            raise RuntimeError(
                "Database schema is not migrated. "
                "Run 'alembic upgrade head' before starting AVA."
            )

        current = row[0]
        if current != self.EXPECTED_REVISION:
            raise RuntimeError(
                f"Database schema is at revision {current!r} but the "
                f"application expects {self.EXPECTED_REVISION!r}. "
                f"Run 'alembic upgrade head' to apply pending migrations."
            )

    def write(self, record: EventRecord) -> bool:
        """Insert event + outbox atomically. Returns False on duplicate."""
        payload_json = json.dumps(record.payload)
        with self._pool.connection() as conn, conn.transaction():
            cur = conn.execute(
                _INSERT_EVENT_SQL,
                (
                    record.event_id,
                    record.camera_id,
                    record.session_id,
                    record.track_id,
                    record.rule_id,
                    record.rule_type,
                    record.class_label,
                    record.confidence,
                    record.source_time_s,
                    _parse_utc(record.emitted_utc),
                    _parse_utc(record.capture_utc),
                    record.time_basis,
                    record.config_version,
                    record.model_ref,
                    record.schema_version,
                    record.frame_index,
                    record.evidence.kind,
                    record.evidence.path,
                    payload_json,
                ),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                _INSERT_OUTBOX_SQL,
                (record.event_id, json.dumps({
                    "event_id": record.event_id,
                    "schema_version": record.schema_version,
                    "camera_id": record.camera_id,
                })),
            )
        return True

    def has_event(self, event_id: str) -> bool:
        """Return True if an event with this ID already exists in the DB."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE event_id = %s", (event_id,)
            ).fetchone()
        return row is not None

    def close(self) -> None:
        """Close the connection pool and release all connections."""
        self._pool.close()
