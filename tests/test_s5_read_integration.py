"""Integration tests for the S5 read side and grant repository (Testcontainers).

Runs against a throwaway PostgreSQL 18 container started by Testcontainers —
never against an existing system database. Each test gets a fresh schema,
migrated to head (revision 0002), so migration + adapter behavior is
exercised end to end.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql

from alembic import command
from ava.api.app import AppConfig, create_app
from ava.application.event_read import find_event, page_events
from ava.application.identity import CallerIdentity
from ava.events import EventRecord, EvidenceLocator
from ava.persistence.grants_postgres import PostgresGrantRepository
from ava.persistence.postgres import PostgresEventSink

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    import shutil
    import subprocess

    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, check=False, timeout=10
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


skip_if_no_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available (required for Testcontainers PostgreSQL)",
)


def _make_record(
    event_id: str,
    camera_id: str = "cam-01",
    emitted_utc: str = "2026-10-27T12:00:00.000Z",
    **kwargs,
) -> EventRecord:
    return EventRecord(
        schema_version="1",
        event_id=event_id,
        camera_id=camera_id,
        session_id="s1",
        track_id=1,
        rule_id="rule-1",
        rule_type="line_crossing",
        class_label="person",
        confidence=0.92,
        source_time_s=12.5,
        emitted_utc=emitted_utc,
        config_version="1",
        model_ref="models/yolo26n.pt",
        evidence=EvidenceLocator(kind="snapshot", path="evidence/cam-01/test.jpg"),
        frame_index=42,
        capture_utc=None,
        time_basis="live_epoch",
        payload={"direction": "left_to_right"},
        **kwargs,
    )


def _connect(pg_info: dict, **kwargs):
    return psycopg.connect(
        host=pg_info["host"],
        port=pg_info["port"],
        dbname=pg_info["dbname"],
        user=pg_info["username"],
        password=pg_info["password"],
        **kwargs,
    )


@skip_if_no_docker
class TestS5ReadSideIntegration:
    """PostgreSQL read paths + grant repository against a fresh schema."""

    @pytest.fixture(autouse=True)
    def isolated_schema(self, pg_container, monkeypatch):
        pg = pg_container
        schema = "ava_s5_" + uuid.uuid4().hex

        with _connect(pg, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        monkeypatch.setenv("AVA_PG_DSN", pg["dsn"])
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

        try:
            command.upgrade(Config("alembic.ini"), "head")
            with _connect(pg) as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            yield pg
        finally:
            with _connect(pg, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    def test_authenticated_api_scopes_and_redacts_database_events(self, isolated_schema):
        class IdentityPolicy:
            def verify(self, token):
                identities = {
                    "tenant-a": CallerIdentity("a", "tenant_a", frozenset({"viewer"})),
                    "tenant-b": CallerIdentity("b", "tenant_b", frozenset({"viewer"})),
                    "site-a": CallerIdentity(
                        "a", "tenant_a", frozenset({"viewer"}), frozenset({"site_1"})
                    ),
                }
                return identities[token]

        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        repo = PostgresGrantRepository(pg["dsn"])
        try:
            for event_id, camera, timestamp in [
                ("evt-3", "cam-a", "2026-10-27T12:00:03.000Z"),
                ("evt-2", "cam-a", "2026-10-27T12:00:02.000Z"),
                ("evt-1", "cam-b", "2026-10-27T12:00:01.000Z"),
            ]:
                sink.write(_make_record(event_id, camera_id=camera, emitted_utc=timestamp))
            repo.set_camera_grant("cam-a", "site_1", ["tenant_a"])
            repo.set_camera_grant("cam-b", None, ["tenant_a"])
            app = create_app(AppConfig(
                identity_policy=IdentityPolicy(), grant_repository=repo, event_repository=sink
            ))
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer tenant-a"}
                first = client.get("/api/v1/events", headers=headers, params={"limit": 2})
                assert first.status_code == 200
                assert [item["event_id"] for item in first.json()["items"]] == ["evt-3", "evt-2"]
                assert "model_ref" not in first.text and "models/yolo26n.pt" not in first.text
                assert "evidence/cam-01/test.jpg" not in first.text
                second = client.get(
                    "/api/v1/events", headers=headers,
                    params={"limit": 2, "cursor": first.json()["next_cursor"]},
                )
                assert [item["event_id"] for item in second.json()["items"]] == ["evt-1"]
                detail = client.get("/api/v1/events/evt-3", headers=headers)
                assert detail.status_code == 200 and "model_ref" not in detail.json()
                other = {"Authorization": "Bearer tenant-b"}
                assert client.get("/api/v1/events", headers=other).json()["items"] == []
                assert client.get("/api/v1/events/evt-3", headers=other).status_code == 404
                restricted = {"Authorization": "Bearer site-a"}
                assert client.get("/api/v1/events/evt-1", headers=restricted).status_code == 404
                visible = client.get("/api/v1/events", headers=restricted).json()["items"]
                assert [item["event_id"] for item in visible] == ["evt-3", "evt-2"]
                repo.revoke_camera_grant("cam-a")
                assert client.get("/api/v1/events/evt-3", headers=headers).status_code == 404
        finally:
            sink.close()
            repo.close()

    def test_migration_0002_creates_grants_table(self, isolated_schema):
        pg = isolated_schema
        with _connect(pg) as conn:
            row = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            assert row[0] == "0002"
            cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'camera_grants'"
                )
            }
            assert {"camera_id", "site_id", "tenant_ids", "updated_at"} <= cols

    def test_check_schema_passes_on_0002(self, isolated_schema):
        pg = isolated_schema
        repo = PostgresGrantRepository(pg["dsn"])
        try:
            repo.check_schema()  # must not raise
        finally:
            repo.close()

    def test_grant_roundtrip_and_read_scoping(self, isolated_schema):
        """Write grants via the admin path, read them back, scope events."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        repo = PostgresGrantRepository(pg["dsn"])
        try:
            # Three cameras: two granted to tenant_a, one not granted at all.
            for i in range(2):
                sink.write(_make_record(f"s5-ev-a{i}", camera_id="cam-a"))
            sink.write(_make_record("s5-ev-b0", camera_id="cam-b"))

            repo.set_camera_grant("cam-a", None, ["tenant_a"])
            # cam-b intentionally left ungranted.

            grants = {g.camera_id: g for g in repo.list_camera_grants()}
            assert set(grants) == {"cam-a"}
            grant = repo.get_camera_grant("cam-a")
            assert grant is not None
            assert grant.tenant_ids == frozenset({"tenant_a"})
            assert repo.get_camera_grant("cam-b") is None

            records = sink.list_all()
            tenant_a = CallerIdentity(
                subject="u1", tenant="tenant_a", roles=frozenset({"viewer"})
            )
            page = page_events(records, grants=grants, caller=tenant_a)
            assert [r.event_id for r in page.items] == ["s5-ev-a1", "s5-ev-a0"]

            detail = find_event(
                sink.find_by_event_id("s5-ev-b0"),
                grants=grants,
                caller=tenant_a,
            )
            assert detail is None  # ungranted camera -> invisible (404 upstream)

            tenant_b = CallerIdentity(
                subject="u2", tenant="tenant_b", roles=frozenset({"viewer"})
            )
            page_b = page_events(records, grants=grants, caller=tenant_b)
            assert page_b.items == []
        finally:
            sink.close()
            repo.close()

    def test_grant_replacement_and_revocation(self, isolated_schema):
        pg = isolated_schema
        repo = PostgresGrantRepository(pg["dsn"])
        try:
            repo.set_camera_grant("cam-x", "site_1", ["t1", "t2"])
            grant = repo.get_camera_grant("cam-x")
            assert grant.site_id == "site_1"
            assert grant.tenant_ids == frozenset({"t1", "t2"})

            # Replace: assignment is overwritten, not appended.
            repo.set_camera_grant("cam-x", None, ["t3"])
            grant = repo.get_camera_grant("cam-x")
            assert grant.site_id is None
            assert grant.tenant_ids == frozenset({"t3"})

            # Revoke: row removed; second revoke reports no-op.
            assert repo.revoke_camera_grant("cam-x") is True
            assert repo.get_camera_grant("cam-x") is None
            assert repo.revoke_camera_grant("cam-x") is False
        finally:
            repo.close()

    def test_read_side_never_returns_evidence_path(self, isolated_schema):
        """D3: the local evidence path must not round-trip into records."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        try:
            record = _make_record("s5-path-1")
            sink.write(record)

            found = sink.find_by_event_id("s5-path-1")
            assert found is not None
            assert found.evidence.kind == "snapshot"
            assert found.evidence.path in (None, "")

            listed = sink.list_all()
            assert all(r.evidence.path in (None, "") for r in listed)
        finally:
            sink.close()

    def test_cursor_pagination_over_db_records(self, isolated_schema):
        """Global order and cursor traversal hold over real DB rows."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        repo = PostgresGrantRepository(pg["dsn"])
        try:
            for i in range(5):
                sink.write(
                    _make_record(
                        f"s5-cur-{i}",
                        camera_id="cam-a",
                        emitted_utc=f"2026-10-27T12:00:{i:02d}.000Z",
                    )
                )
            repo.set_camera_grant("cam-a", None, ["tenant_a"])

            from ava.application.identity import CallerIdentity

            caller = CallerIdentity(
                subject="u1", tenant="tenant_a", roles=frozenset({"viewer"})
            )
            grants = {g.camera_id: g for g in repo.list_camera_grants()}
            records = sink.list_all()

            seen: list[str] = []
            cursor = None
            while True:
                page = page_events(
                    records, grants=grants, caller=caller, cursor=cursor, limit=2
                )
                seen.extend(r.event_id for r in page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor

            # Newest first; no overlap, no gaps.
            assert seen == [
                "s5-cur-4", "s5-cur-3", "s5-cur-2", "s5-cur-1", "s5-cur-0"
            ]
        finally:
            sink.close()
            repo.close()
