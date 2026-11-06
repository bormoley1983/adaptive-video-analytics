"""Integration tests for the S5 read side, grant repository and camera
registry (Testcontainers).

Runs against a throwaway PostgreSQL 18 container started by Testcontainers —
never against an existing system database. Each test gets a fresh schema,
migrated to head (revision 0003), so migration + adapter behavior is
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
from ava.api.app import AppConfig, compose_postgres_app, create_app
from ava.application.event_read import find_event, page_events
from ava.application.identity import CallerIdentity
from ava.events import EventRecord, EvidenceLocator
from ava.persistence.cameras_postgres import PostgresCameraRepository
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
        cam_repo = PostgresCameraRepository(pg["dsn"])
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
                identity_policy=IdentityPolicy(), grant_repository=repo, event_repository=sink,
                camera_repository=cam_repo,
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
            cam_repo.close()

    def test_migration_0003_creates_cameras_table(self, isolated_schema):
        pg = isolated_schema
        with _connect(pg) as conn:
            row = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            assert row[0] == "0003"
            # camera_grants table from 0002 still present.
            grant_cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'camera_grants'"
                )
            }
            assert {"camera_id", "site_id", "tenant_ids", "updated_at"} <= grant_cols
            # cameras table from 0003.
            cam_cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'cameras'"
                )
            }
            assert {"camera_id", "site_id", "deleted_at", "updated_at"} <= cam_cols

    def test_check_schema_passes_on_0003(self, isolated_schema):
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

    def test_event_write_creates_camera_row(self, isolated_schema):
        """F-1-S6 (ADR-19): event write ensures a camera registry row exists.

        A camera that emits events but has no grant still gets a cameras row
        (site_id NULL) so the event-to-camera mapping is complete. The upsert
        is idempotent — writing multiple events for the same camera does not
        duplicate rows or overwrite an existing site_id.
        """
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        cam_repo = PostgresCameraRepository(pg["dsn"])
        try:
            # Write an event for a camera with no grant.
            sink.write(_make_record("evt-cam-1", camera_id="cam-orphan"))

            # Camera row exists with site_id NULL (no grant to supply site).
            cam = cam_repo.get_camera("cam-orphan")
            assert cam is not None
            assert cam.camera_id == "cam-orphan"
            assert cam.site_id is None
            assert cam.deleted_at is None

            # Writing another event for the same camera does not duplicate.
            sink.write(_make_record("evt-cam-2", camera_id="cam-orphan"))
            cameras = cam_repo.list_cameras()
            orphan_rows = [c for c in cameras if c.camera_id == "cam-orphan"]
            assert len(orphan_rows) == 1

            # A grant upsert sets site_id (grants are canonical site source).
            from ava.persistence.grants_postgres import PostgresGrantRepository

            grant_repo = PostgresGrantRepository(pg["dsn"])
            try:
                grant_repo.set_camera_grant_and_reactivate_camera(
                    "cam-orphan", "site_9", ["tenant_a"]
                )
                cam = cam_repo.get_camera("cam-orphan")
                assert cam is not None
                assert cam.site_id == "site_9"
            finally:
                grant_repo.close()
        finally:
            sink.close()
            cam_repo.close()

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

    def test_migration_backfill_populates_cameras_from_grants(self, pg_container, monkeypatch):
        """P1 #1: upgrading from 0002 with existing data backfills cameras.

        Seeds a schema at revision 0002 (with grant rows AND event rows for
        cameras that have no grant), then runs the ACTUAL alembic upgrade to
        head (0003) and verifies the cameras table is populated from both
        camera_grants (with site_id) and events (site_id NULL) by the
        migration's own backfill SQL.
        """
        pg = pg_container
        schema = "ava_s5_bf_" + uuid.uuid4().hex

        with _connect(pg, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        monkeypatch.setenv("AVA_PG_DSN", pg["dsn"])
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

        try:
            # Step 1: migrate to 0002 (pre-camera-registry state).
            command.upgrade(Config("alembic.ini"), "0002")
            with _connect(pg) as conn:
                rev = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                assert rev[0] == "0002"
                # Seed grant rows (pre-0003 data).
                conn.execute(
                    "INSERT INTO camera_grants (camera_id, site_id, tenant_ids, updated_at) "
                    "VALUES ('cam-bf-a', 'site_1', ARRAY['tenant_a'], now()),"
                    "('cam-bf-b', NULL, ARRAY['tenant_b'], now())"
                )
                # Seed event rows for a camera with NO grant (e.g. after revocation).
                # This camera should get a cameras row with site_id NULL from the
                # events backfill source.
                conn.execute(
                    "INSERT INTO events (event_id, camera_id, session_id, track_id, "
                    "rule_id, rule_type, class_label, confidence, source_time_s, "
                    "emitted_utc, time_basis, config_version, model_ref, schema_version, "
                    "frame_index, evidence_kind, payload) "
                    "VALUES ('evt-bf-orphan', 'cam-bf-orphan', 's1', 1, "
                    "'rule-1', 'line_crossing', 'person', 0.9, 12.5, "
                    "now(), 'live_epoch', '1', 'models/yolo26n.pt', '1', "
                    "42, 'snapshot', '{}')"
                )

            # Step 2: run the ACTUAL upgrade to head (0003).
            command.upgrade(Config("alembic.ini"), "head")

            # Step 3: verify the migration's backfill populated cameras from
            # BOTH sources: grants (with site_id) and events (site_id NULL).
            with _connect(pg) as conn:
                rev = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                assert rev[0] == "0003"
                rows = conn.execute(
                    "SELECT camera_id, site_id, deleted_at FROM cameras ORDER BY camera_id"
                ).fetchall()
            # Three cameras: two from grants, one from events (no grant).
            assert len(rows) == 3
            assert rows[0][0] == "cam-bf-a"
            assert rows[0][1] == "site_1"  # site from grant
            assert rows[0][2] is None
            assert rows[1][0] == "cam-bf-b"
            assert rows[1][1] is None  # grant had NULL site
            assert rows[1][2] is None
            assert rows[2][0] == "cam-bf-orphan"
            assert rows[2][1] is None  # no grant, so site_id is NULL
            assert rows[2][2] is None

            # Verify via the real adapter.
            cam_repo = PostgresCameraRepository(pg["dsn"])
            try:
                cameras = {c.camera_id: c for c in cam_repo.list_cameras()}
                assert set(cameras) == {"cam-bf-a", "cam-bf-b", "cam-bf-orphan"}
                assert cameras["cam-bf-a"].site_id == "site_1"
                assert cameras["cam-bf-orphan"].site_id is None
            finally:
                cam_repo.close()
        finally:
            with _connect(pg, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    def test_grant_write_creates_and_reactivates_camera(self, isolated_schema):
        pg = isolated_schema
        cam_repo = PostgresCameraRepository(pg["dsn"])
        grant_repo = PostgresGrantRepository(pg["dsn"])
        try:
            grant_repo.set_camera_grant("cam-grant", "site_1", ["tenant_a"])
            assert cam_repo.get_camera("cam-grant").site_id == "site_1"
            assert cam_repo.delete_camera("cam-grant")

            grant_repo.set_camera_grant("cam-grant", None, ["tenant_b"])
            camera = cam_repo.get_camera("cam-grant")
            assert camera is not None
            assert camera.site_id is None
            assert grant_repo.get_camera_grant("cam-grant").tenant_ids == frozenset({"tenant_b"})
        finally:
            cam_repo.close()
            grant_repo.close()

    def test_camera_soft_delete_and_reactivation_via_real_adapter(self, isolated_schema):
        """P1 #3: real PostgresCameraRepository soft-delete + re-activation."""
        pg = isolated_schema
        cam_repo = PostgresCameraRepository(pg["dsn"])
        grant_repo = PostgresGrantRepository(pg["dsn"])
        try:
            # Create a camera via upsert.
            cam_repo.upsert_camera("cam-real", "site_1")
            cam = cam_repo.get_camera("cam-real")
            assert cam is not None
            assert cam.site_id == "site_1"
            assert cam.deleted_at is None

            # Soft-delete it.
            deleted = cam_repo.delete_camera("cam-real")
            assert deleted is True
            # get_camera returns None for soft-deleted cameras.
            assert cam_repo.get_camera("cam-real") is None
            # find_camera_by_camera_id returns the row including deleted_at.
            full = cam_repo.find_camera_by_camera_id("cam-real")
            assert full is not None
            assert full.deleted_at is not None

            # Re-activate via the atomic grant + camera path.
            grant_repo.set_camera_grant_and_reactivate_camera(
                "cam-real", "site_1", ["tenant_a"]
            )
            cam = cam_repo.get_camera("cam-real")
            assert cam is not None
            assert cam.deleted_at is None
            # Grant is also set.
            grant = grant_repo.get_camera_grant("cam-real")
            assert grant is not None
            assert grant.tenant_ids == frozenset({"tenant_a"})

            # Double-delete: second delete returns False (no active row).
            assert cam_repo.delete_camera("cam-real") is True
            assert cam_repo.delete_camera("cam-real") is False
        finally:
            cam_repo.close()
            grant_repo.close()

    def test_compose_postgres_app_exposes_camera_routes(self, isolated_schema):
        """P1 #3: compose_postgres_app wires all three repositories.

        Verifies the explicit supported composition path: DELETE and GET
        /cameras/{camera_id} work against real Postgres adapters (not fakes).
        Uses compose_postgres_app (the production entry point) which owns
        the pool lifecycle — pools are closed on app shutdown.
        """
        pg = isolated_schema

        # Seed data via standalone adapters (before composing the app).
        seed_cam = PostgresCameraRepository(pg["dsn"])
        seed_grant = PostgresGrantRepository(pg["dsn"])
        seed_sink = PostgresEventSink(pg["dsn"])
        try:
            seed_cam.upsert_camera("cam-compose", "site_1")
            seed_grant.set_camera_grant("cam-compose", "site_1", ["tenant_a"])
            seed_sink.write(_make_record("evt-compose-1", camera_id="cam-compose"))
        finally:
            seed_cam.close()
            seed_grant.close()
            seed_sink.close()

        class IdentityPolicy:
            def verify(self, token):
                identities = {
                    "admin": CallerIdentity(
                        "u1", "tenant_a", frozenset({"viewer", "admin"})
                    ),
                    "viewer": CallerIdentity(
                        "u2", "tenant_a", frozenset({"viewer"})
                    ),
                }
                return identities[token]

        # Use the production composition entry point.
        app = compose_postgres_app(pg["dsn"], identity_policy=IdentityPolicy())
        with TestClient(app) as client:
            admin = {"Authorization": "Bearer admin"}
            viewer = {"Authorization": "Bearer viewer"}

            # GET /cameras/{id} — admin sees the camera.
            resp = client.get("/api/v1/cameras/cam-compose", headers=admin)
            assert resp.status_code == 200
            body = resp.json()
            assert body["camera_id"] == "cam-compose"
            assert body["site_id"] == "site_1"
            assert body["deleted_at"] is None

            # GET /cameras/{id} — non-admin gets 401.
            resp = client.get("/api/v1/cameras/cam-compose", headers=viewer)
            assert resp.status_code == 401

            # DELETE /cameras/{id} — admin soft-deletes.
            resp = client.delete("/api/v1/cameras/cam-compose", headers=admin)
            assert resp.status_code == 204

            # Camera is now hidden from inventory.
            resp = client.get("/api/v1/cameras", headers=admin)
            assert resp.status_code == 200
            assert [c["camera_id"] for c in resp.json()["cameras"]] == []

            # GET /cameras/{id} — admin sees deleted_at set.
            resp = client.get("/api/v1/cameras/cam-compose", headers=admin)
            assert resp.status_code == 200
            assert resp.json()["deleted_at"] is not None

            # DELETE again — 404 (no active camera).
            resp = client.delete("/api/v1/cameras/cam-compose", headers=admin)
            assert resp.status_code == 404

            # Re-activate via grant upsert (atomic path).
            resp = client.put(
                "/api/v1/grants/cam-compose",
                headers=admin,
                json={"site_id": "site_1", "tenant_ids": ["tenant_a"]},
            )
            assert resp.status_code == 200

            # Camera is back in inventory.
            resp = client.get("/api/v1/cameras", headers=admin)
            assert [c["camera_id"] for c in resp.json()["cameras"]] == ["cam-compose"]

            # Events are accessible again.
            resp = client.get("/api/v1/events/evt-compose-1", headers=admin)
            assert resp.status_code == 200

        # Pools are closed after TestClient context exits (lifespan shutdown).
        # Verify the app's repositories are no longer usable.
        for repo in (
            app.state.event_repository,
            app.state.grant_repository,
            app.state.camera_repository,
        ):
            with pytest.raises(psycopg.OperationalError):
                if hasattr(repo, "check_connection"):
                    repo.check_connection()
                elif hasattr(repo, "get_camera_grant"):
                    repo.get_camera_grant("any")
                else:
                    repo.list_cameras()

    def test_compose_postgres_app_rejects_outdated_schema(self, pg_container, monkeypatch):
        """P1 #1 (this round): compose_postgres_app rejects a 0002 schema.

        Seeds a database at revision 0002 (no cameras table) and verifies that
        compose_postgres_app raises RuntimeError during composition — the app
        is never returned, so it cannot report ready against an unmigrated DB.
        """
        pg = pg_container
        schema = "ava_s5_rej_" + uuid.uuid4().hex

        with _connect(pg, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        monkeypatch.setenv("AVA_PG_DSN", pg["dsn"])
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

        try:
            # Migrate to 0002 only (no cameras table).
            command.upgrade(Config("alembic.ini"), "0002")
            with _connect(pg) as conn:
                rev = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                assert rev[0] == "0002"

            # compose_postgres_app must reject the outdated schema.
            with pytest.raises(RuntimeError, match="0002.*0003|not migrated"):
                compose_postgres_app(pg["dsn"])
        finally:
            with _connect(pg, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    def test_compose_postgres_app_readiness_reports_database(self, isolated_schema):
        """P1 #1 (this round): readiness contributor reports database state.

        After compose_postgres_app succeeds, /health/ready includes a required
        'database' dependency in READY state. When the pool is closed (simulating
        DB unavailability), readiness reports UNAVAILABLE and ready=False.
        """
        pg = isolated_schema
        app = compose_postgres_app(pg["dsn"])
        with TestClient(app) as client:
            # Readiness should report ready with a database dependency.
            resp = client.get("/api/v1/health/ready")
            assert resp.status_code == 200
            body = resp.json()
            assert body["ready"] is True
            deps = {d["name"]: d for d in body["dependencies"]}
            assert "database" in deps
            assert deps["database"]["state"] == "ready"

        # After shutdown, the pools are closed. A new readiness check would
        # report unavailable (but we can't call it without a running app).
        # Verify the pool is actually closed by attempting a connection.
        with pytest.raises(psycopg.OperationalError):
            app.state.event_repository.check_connection()

    def test_atomic_reactivation_rolls_back_on_failure(self, isolated_schema):
        """P1 (failure injection): atomic grant + re-activation rolls back.

        Injects a failure after the grant write but before the camera upsert
        within the same transaction. Verifies that BOTH changes are rolled
        back: the grant is unchanged and the camera remains soft-deleted.
        """
        pg = isolated_schema
        cam_repo = PostgresCameraRepository(pg["dsn"])
        grant_repo = PostgresGrantRepository(pg["dsn"])
        try:
            # Set up: camera exists, is soft-deleted, has an old grant.
            cam_repo.upsert_camera("cam-atomic", "site_1")
            grant_repo.set_camera_grant("cam-atomic", "site_1", ["tenant_old"])
            assert cam_repo.delete_camera("cam-atomic") is True

            # Capture pre-failure state.
            old_grant = grant_repo.get_camera_grant("cam-atomic")
            assert old_grant is not None
            assert old_grant.tenant_ids == frozenset({"tenant_old"})
            assert cam_repo.get_camera("cam-atomic") is None  # soft-deleted

            # Inject a real database failure into the production method's
            # second write (camera upsert) by making it fail with a
            # constraint violation. The grant write (first statement in the
            # same transaction) succeeds, then the camera upsert fails —
            # PostgreSQL rolls back the entire transaction.
            #
            # We achieve this by temporarily adding a trigger on cameras
            # that raises an exception for the specific camera_id. The
            # production method runs its real code path: one connection,
            # one transaction, grant upsert then camera upsert.
            with _connect(pg) as conn:
                conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION _test_fail_cameras()
                    RETURNS TRIGGER AS $$
                    BEGIN
                        IF NEW.camera_id = 'cam-atomic' THEN
                            RAISE EXCEPTION 'injected: camera upsert failed';
                        END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER _test_cameras_fail
                    BEFORE INSERT OR UPDATE ON cameras
                    FOR EACH ROW EXECUTE FUNCTION _test_fail_cameras()
                    """
                )

            try:
                # Call the PRODUCTION atomic method — it runs its real code.
                with pytest.raises(Exception, match="injected"):
                    grant_repo.set_camera_grant_and_reactivate_camera(
                        "cam-atomic", "site_2", ["tenant_new"]
                    )
            finally:
                # Remove the trigger so subsequent operations work.
                with _connect(pg) as conn:
                    conn.execute("DROP TRIGGER IF EXISTS _test_cameras_fail ON cameras")
                    conn.execute("DROP FUNCTION IF EXISTS _test_fail_cameras()")

            # Verify: BOTH changes rolled back (production method's transaction).
            # Grant is unchanged (still tenant_old, not tenant_new).
            grant = grant_repo.get_camera_grant("cam-atomic")
            assert grant is not None
            assert grant.tenant_ids == frozenset({"tenant_old"})
            assert grant.site_id == "site_1"  # not site_2

            # Camera is still soft-deleted (not re-activated).
            assert cam_repo.get_camera("cam-atomic") is None
            full = cam_repo.find_camera_by_camera_id("cam-atomic")
            assert full is not None
            assert full.deleted_at is not None

            # Verify the production method still works after the failure.
            grant_repo.set_camera_grant_and_reactivate_camera(
                "cam-atomic", "site_2", ["tenant_new"]
            )
            grant = grant_repo.get_camera_grant("cam-atomic")
            assert grant is not None
            assert grant.tenant_ids == frozenset({"tenant_new"})
            cam = cam_repo.get_camera("cam-atomic")
            assert cam is not None
            assert cam.deleted_at is None
        finally:
            cam_repo.close()
            grant_repo.close()
