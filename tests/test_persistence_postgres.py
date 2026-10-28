"""Integration tests for PostgreSQL event persistence (Testcontainers)."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql

from alembic import command
from ava.events import EventRecord, EvidenceLocator
from ava.persistence.postgres import PostgresEventSink

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    """Check if Docker is available for Testcontainers."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


skip_if_no_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available (required for Testcontainers PostgreSQL)",
)


def _make_record(event_id: str = "cam-01/s1/track-1/rule-1/crossing/1") -> EventRecord:
    return EventRecord(
        schema_version="1",
        event_id=event_id,
        camera_id="cam-01",
        session_id="s1",
        track_id=1,
        rule_id="rule-1",
        rule_type="line_crossing",
        class_label="person",
        confidence=0.92,
        source_time_s=12.5,
        emitted_utc="2026-10-27T12:00:00.000Z",
        config_version="1",
        model_ref="models/yolo26n.pt",
        evidence=EvidenceLocator(kind="snapshot", path="evidence/cam-01/test.jpg"),
        frame_index=42,
        capture_utc="2026-10-27T12:00:00.000Z",
        time_basis="live_epoch",
        payload={"direction": "left_to_right"},
    )


def _connect(pg_info: dict, **kwargs):
    """Connect using keyword format to avoid psycopg v3 URL parsing issues."""
    return psycopg.connect(
        host=pg_info["host"],
        port=pg_info["port"],
        dbname=pg_info["dbname"],
        user=pg_info["username"],
        password=pg_info["password"],
        **kwargs,
    )


@skip_if_no_docker
class TestPostgresEventSinkIntegration:
    """End-to-end tests against a Testcontainers PostgreSQL instance."""

    @pytest.fixture(autouse=True)
    def isolated_schema(self, pg_container, monkeypatch):
        """Create a fresh schema per test and run Alembic migrations into it."""
        pg = pg_container
        schema = "ava_test_" + uuid.uuid4().hex

        with _connect(pg, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        monkeypatch.setenv("AVA_PG_DSN", pg["dsn"])
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

        try:
            command.upgrade(Config("alembic.ini"), "head")
            yield pg
        finally:
            with _connect(pg, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    def test_insert_event_and_outbox_atomically(self, isolated_schema) -> None:
        """A new event creates both an events row and an outbox row."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        record = _make_record(event_id="itest-atomic-1")
        try:
            result = sink.write(record)
            assert result is True

            with _connect(pg) as conn:
                row = conn.execute(
                    "SELECT event_id, camera_id, rule_type, confidence "
                    "FROM events WHERE event_id = %s",
                    (record.event_id,),
                ).fetchone()
                assert row is not None
                assert row[0] == record.event_id
                assert row[1] == "cam-01"
                assert row[2] == "line_crossing"
                assert abs(row[3] - 0.92) < 1e-6

                outbox = conn.execute(
                    "SELECT event_id, aggregate_type FROM outbox WHERE event_id = %s",
                    (record.event_id,),
                ).fetchone()
                assert outbox is not None
                assert outbox[0] == record.event_id
                assert outbox[1] == "event"
        finally:
            sink.close()

    def test_duplicate_event_is_idempotent(self, isolated_schema) -> None:
        """Writing the same event_id twice returns False the second time."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        record = _make_record(event_id="itest-dedup-1")
        try:
            assert sink.write(record) is True
            assert sink.write(record) is False

            with _connect(pg) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_id = %s",
                    (record.event_id,),
                ).fetchone()[0]
                assert count == 1

                outbox_count = conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE event_id = %s",
                    (record.event_id,),
                ).fetchone()[0]
                assert outbox_count == 1
        finally:
            sink.close()

    def test_multiple_distinct_events(self, isolated_schema) -> None:
        """Ten distinct events all persist with matching outbox rows."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        try:
            for i in range(10):
                record = _make_record(event_id=f"itest-batch-{i}")
                assert sink.write(record) is True

            with _connect(pg) as conn:
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_id LIKE 'itest-batch-%'"
                ).fetchone()[0]
                assert event_count == 10

                outbox_count = conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE event_id LIKE 'itest-batch-%'"
                ).fetchone()[0]
                assert outbox_count == 10
        finally:
            sink.close()

    def test_payload_stored_as_jsonb(self, isolated_schema) -> None:
        """The variable payload is stored as jsonb and is queryable."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        record = EventRecord(
            schema_version="1",
            event_id="itest-jsonb-1",
            camera_id="cam-01",
            session_id="s1",
            track_id=1,
            rule_id="rule-1",
            rule_type="line_crossing",
            class_label="person",
            confidence=0.92,
            source_time_s=12.5,
            emitted_utc="2026-10-27T12:00:00.000Z",
            config_version="1",
            model_ref="models/yolo26n.pt",
            evidence=EvidenceLocator(kind="snapshot", path="evidence/cam-01/test.jpg"),
            frame_index=42,
            capture_utc="2026-10-27T12:00:00.000Z",
            time_basis="live_epoch",
            payload={"direction": "left_to_right", "count": 3},
        )
        try:
            assert sink.write(record) is True

            with _connect(pg) as conn:
                row = conn.execute(
                    "SELECT payload FROM events WHERE event_id = %s",
                    (record.event_id,),
                ).fetchone()
                assert row is not None
                payload = row[0]
                assert payload["direction"] == "left_to_right"
                assert payload["count"] == 3
        finally:
            sink.close()

    def test_evidence_fields_stored(self, isolated_schema) -> None:
        """Evidence kind and path are stored in typed columns."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        record = _make_record(event_id="itest-evidence-1")
        try:
            assert sink.write(record) is True

            with _connect(pg) as conn:
                row = conn.execute(
                    "SELECT evidence_kind, evidence_path FROM events "
                    "WHERE event_id = %s",
                    (record.event_id,),
                ).fetchone()
                assert row is not None
                assert row[0] == "snapshot"
                assert row[1] == "evidence/cam-01/test.jpg"
        finally:
            sink.close()

    def test_bad_dsn_raises_on_init(self) -> None:
        """A bad DSN fails fast at construction time."""
        with pytest.raises(psycopg.OperationalError):
            PostgresEventSink("postgresql://bad:***@localhost:9999/nodb")

    # ------------------------------------------------------------------
    # Schema type and migration tests
    # ------------------------------------------------------------------

    def test_payload_columns_are_jsonb(self, isolated_schema) -> None:
        """Both payload columns are PostgreSQL jsonb, not json."""
        pg = isolated_schema
        # Determine the active schema from the search_path.
        with _connect(pg) as conn:
            schema = conn.execute("SHOW search_path").fetchone()[0].split(",")[0].strip()
            rows = conn.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE column_name = 'payload' "
                "AND table_name IN ('events', 'outbox') "
                "AND table_schema = %s "
                "ORDER BY table_name",
                (schema,),
            ).fetchall()

        # Both events.payload and outbox.payload must be jsonb.
        assert len(rows) == 2
        for table, column, data_type in rows:
            assert data_type == "jsonb", (
                f"{table}.{column} is {data_type!r}, expected 'jsonb'"
            )

    def test_alembic_version_matches_expected(self, isolated_schema) -> None:
        """The alembic_version table records the expected revision."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        try:
            # check_schema should pass (migration was run in fixture).
            sink.check_schema()

            with _connect(pg) as conn:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                assert row is not None
                assert row[0] == PostgresEventSink.EXPECTED_REVISION
        finally:
            sink.close()

    def test_check_schema_fails_on_unmigrated_db(self, pg_container, monkeypatch) -> None:
        """check_schema raises a clear error when no migration has run."""
        pg = pg_container
        schema = "ava_test_" + uuid.uuid4().hex

        with _connect(pg, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        monkeypatch.setenv("AVA_PG_DSN", pg["dsn"])
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

        try:
            sink = PostgresEventSink(pg["dsn"])
            try:
                with pytest.raises(RuntimeError, match="not migrated"):
                    sink.check_schema()
            finally:
                sink.close()
        finally:
            with _connect(pg, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    def test_migration_downgrade_and_upgrade(self, pg_container, monkeypatch) -> None:
        """alembic downgrade base drops all tables; upgrade head recreates them."""
        pg = pg_container
        schema = "ava_test_" + uuid.uuid4().hex

        with _connect(pg, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        monkeypatch.setenv("AVA_PG_DSN", pg["dsn"])
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

        try:
            # Upgrade to head.
            command.upgrade(Config("alembic.ini"), "head")

            with _connect(pg) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = %s",
                        (schema,),
                    ).fetchall()
                }
            assert "events" in tables
            assert "outbox" in tables

            # Downgrade to base (drops everything).
            command.downgrade(Config("alembic.ini"), "base")

            with _connect(pg) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = %s",
                        (schema,),
                    ).fetchall()
                }
            assert "events" not in tables
            assert "outbox" not in tables

            # Upgrade again — should work cleanly.
            command.upgrade(Config("alembic.ini"), "head")

            with _connect(pg) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = %s",
                        (schema,),
                    ).fetchall()
                }
            assert "events" in tables
            assert "outbox" in tables
        finally:
            with _connect(pg, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    # ------------------------------------------------------------------
    # Atomic rollback test
    # ------------------------------------------------------------------

    def test_outbox_failure_rolls_back_event(self, isolated_schema) -> None:
        """If the outbox insert fails, the event insert is rolled back."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        record = _make_record(event_id="itest-rollback-1")

        try:
            # Drop the outbox table to force the outbox insert to fail.
            with _connect(pg, autocommit=True) as conn:
                conn.execute("DROP TABLE outbox")

            with pytest.raises(psycopg.errors.UndefinedTable):
                sink.write(record)

            # The event row must not exist (transaction was rolled back).
            with _connect(pg) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_id = %s",
                    (record.event_id,),
                ).fetchone()[0]
                assert count == 0, (
                    f"Event {record.event_id!r} should have been rolled back"
                )
        finally:
            sink.close()
