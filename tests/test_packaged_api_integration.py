"""Packaged API acceptance: the installed AVA package serves the read API.

Proves that ``ava`` resolves from its installed site-packages location (not
the source checkout) and that the public authenticated API — health, version,
capabilities, events, cameras — works end to end against a throwaway
PostgreSQL with real Alembic migrations, a generated sanitized synthetic
event fixture, trusted grants, and a test-only development identity double.

Redaction (D3): ``model_ref``, local evidence paths, payloads and frame
indexes never appear in any response body. A dev identity is not a real
identity provider; this test does not validate one.

Reproducible invocation:
    make test-packaged          # from the task worktree root

The Makefile target builds an isolated virtualenv with the API-relevant
subset of the locked stack, installs the package non-editably, and runs this
suite with ``-p no:cacheprovider --override-ini pythonpath=`` so pytest does
not prepend the source checkout to ``sys.path`` (pyproject.toml sets
``pythonpath = ["src"]``; that would make every test in this process import
``ava`` from the checkout regardless of what is installed). The first test
in the class asserts, inside the test process, that the imported module
resolves inside the non-editable installation and fails when the packaged
gate lacks it.

The suite is selected by the ``packaged`` marker, so the standard
``make test-integration`` gate never runs these tests in a development
(editable-only) environment. When Docker is unavailable the whole class is
skipped; ``make test-packaged`` then fails the gate because its required
service is missing (a green exit would mean nothing was verified).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql

import ava
from alembic import command
from ava.api.app import AppConfig, create_app
from ava.application.identity import CallerIdentity, TokenValidationError
from ava.events import EventRecord, EvidenceLocator
from ava.persistence.cameras_postgres import PostgresCameraRepository
from ava.persistence.grants_postgres import PostgresGrantRepository
from ava.persistence.postgres import PostgresEventSink

pytestmark = [pytest.mark.integration, pytest.mark.packaged]


def _docker_available() -> bool:
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


# ── Generated sanitized synthetic fixture (explicitly NOT a real event) ─────


def make_fixture_event(
    event_id: str,
    camera_id: str = "cam-01",
    emitted_utc: str = "2026-10-27T12:00:00.000Z",
    time_basis: str = "live_epoch",
) -> EventRecord:
    """One synthetic event carrying every field the redaction rules must drop."""
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
        evidence=EvidenceLocator(kind="snapshot", path="evidence/cam-01/fx.jpg"),
        frame_index=42,
        capture_utc=None,
        time_basis=time_basis,
        payload={"direction": "left_to_right"},
    )


def make_fixture_event_file_only(
    event_id: str, camera_id: str = "cam-01", emitted_utc: str = "2026-10-27T12:00:05.000Z"
) -> EventRecord:
    """Synthetic file-only run event: time_basis must serialize as null."""
    return make_fixture_event(
        event_id, camera_id=camera_id, emitted_utc=emitted_utc, time_basis="file_pts_only"
    )


# ── Test-only development identity (never a production auth bypass) ─────────

_DEV_TOKENS = {
    "dev-tenant-a": CallerIdentity("a", "tenant_a", frozenset({"viewer"})),
    "dev-tenant-b": CallerIdentity("b", "tenant_b", frozenset({"viewer"})),
    "dev-site-a": CallerIdentity(
        "a", "tenant_a", frozenset({"viewer"}), frozenset({"site_1"})
    ),
    "dev-admin": CallerIdentity("adm", "tenant_a", frozenset({"admin", "viewer"})),
}


class DevIdentityPolicy:
    """Hardcoded dev identity confined to this test harness."""

    def verify(self, token: str) -> CallerIdentity:
        try:
            return _DEV_TOKENS[token]
        except KeyError:
            raise TokenValidationError("invalid_token", "unknown development token") from None


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
class TestPackagedApiIntegration:
    """Installed package + real PostgreSQL + dev identity through the API."""

    @pytest.fixture(autouse=True)
    def isolated_schema(self, pg_container, monkeypatch):
        pg = pg_container
        schema = "ava_pkg_" + uuid.uuid4().hex

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

    @pytest.fixture()
    def api_client(self, isolated_schema):
        """App wired with real Postgres repositories and the dev identity."""
        pg = isolated_schema
        sink = PostgresEventSink(pg["dsn"])
        repo = PostgresGrantRepository(pg["dsn"])
        cam_repo = PostgresCameraRepository(pg["dsn"])
        try:
            app = create_app(
                AppConfig(
                    package_version="0.1.0",
                    contract_version="1.0.0",
                    identity_policy=DevIdentityPolicy(),
                    grant_repository=repo,
                    event_repository=sink,
                    camera_repository=cam_repo,
                )
            )
            yield TestClient(app), sink, repo
        finally:
            sink.close()
            repo.close()
            cam_repo.close()

    def _seed(self, sink: PostgresEventSink, repo: PostgresGrantRepository) -> None:
        for event_id, camera, timestamp in [
            ("pkg-evt-3", "cam-a", "2026-10-27T12:00:03.000Z"),
            ("pkg-evt-2", "cam-a", "2026-10-27T12:00:02.000Z"),
            ("pkg-evt-1", "cam-b", "2026-10-27T12:00:01.000Z"),
        ]:
            sink.write(make_fixture_event(event_id, camera_id=camera, emitted_utc=timestamp))
        # Older than all events above; exercises the file_pts_only -> null rule.
        sink.write(
            make_fixture_event_file_only(
                "pkg-evt-0", camera_id="cam-a", emitted_utc="2026-10-27T11:59:59.000Z"
            )
        )
        repo.set_camera_grant("cam-a", "site_1", ["tenant_a"])
        repo.set_camera_grant("cam-b", None, ["tenant_a"])

    def test_imports_resolve_from_installed_location(self):
        """The ava module under test must come from the non-editable install.

        Asserted in this test process (not a subprocess): ``ava`` was imported
        at module load time above, so ``ava.__file__`` is exactly the code the
        API tests below exercise. It must live under site-packages, and the
        distribution backing it must not be an editable install (checked via
        ``direct_url.json``). This fails — it does not skip — when the
        packaged gate lacks the intended installation.
        """
        import importlib.metadata

        assert "site-packages" in ava.__file__, (
            f"ava imported from {ava.__file__!r}; expected the installed "
            "location. Run via 'make test-packaged', which overrides pytest's "
            "pythonpath so the checkout is not on sys.path."
        )

        dist = None
        for candidate in importlib.metadata.distributions():
            if candidate.name != "ava":
                continue
            base = Path(candidate.locate_file(""))
            if "site-packages" not in str(base):
                continue
            direct_url = (
                base / f"{candidate.metadata['Name']}-{candidate.version}.dist-info"
                / "direct_url.json"
            )
            if direct_url.is_file():
                info = json.loads(direct_url.read_text())
                if info.get("dir_info", {}).get("editable"):
                    continue
            dist = candidate
            break

        assert dist is not None, (
            f"ava resolved to {ava.__file__!r} but no non-editable ava "
            "distribution was found under site-packages; the packaged gate "
            "requires 'pip install <checkout>' (non-editable) in an isolated venv"
        )

    def test_health_version_and_capabilities(self, api_client):
        client, sink, repo = api_client
        self._seed(sink, repo)
        headers = {"Authorization": "Bearer dev-tenant-a"}

        live = client.get("/api/v1/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "alive"}

        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True

        version = client.get("/api/v1/version", headers=headers)
        assert version.status_code == 200
        body = version.json()
        assert body["package_version"] == "0.1.0"
        assert body["contract_version"] == "1.0.0"
        assert "python_version" in body

        caps = client.get("/api/v1/capabilities", headers=headers)
        assert caps.status_code == 200
        entries = caps.json()["capabilities"]
        assert [c["id"] for c in entries] == ["events.read"]
        entry = entries[0]
        assert entry["available"] is True
        assert entry["limits"]["max_page_size"] == 200

    def test_unauthenticated_requests_rejected(self, api_client):
        client, _, _ = api_client
        for path in ("/api/v1/version", "/api/v1/capabilities", "/api/v1/events"):
            r = client.get(path)
            assert r.status_code == 401, f"{path} without token must be 401"
            assert r.json()["code"] in ("missing_token", "invalid_token")

    def test_event_retrieval_scoped_and_paginated(self, api_client):
        client, sink, repo = api_client
        self._seed(sink, repo)
        headers = {"Authorization": "Bearer dev-tenant-a"}

        first = client.get("/api/v1/events", headers=headers, params={"limit": 2})
        assert first.status_code == 200
        body = first.json()
        assert [i["event_id"] for i in body["items"]] == ["pkg-evt-3", "pkg-evt-2"]
        assert body["next_cursor"] is not None

        second = client.get(
            "/api/v1/events",
            headers=headers,
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert [i["event_id"] for i in second.json()["items"]] == ["pkg-evt-1", "pkg-evt-0"]

        # The page was full, so a cursor is issued; traversing it yields the
        # final (partial) page and then stops.
        third = client.get(
            "/api/v1/events",
            headers=headers,
            params={"limit": 2, "cursor": second.json()["next_cursor"]},
        )
        assert [i["event_id"] for i in third.json()["items"]] == []
        assert third.json()["next_cursor"] is None

        detail = client.get("/api/v1/events/pkg-evt-3", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["event_id"] == "pkg-evt-3"
        assert detail.json()["camera_id"] == "cam-a"

    def test_tenant_and_site_access_boundaries(self, api_client):
        client, sink, repo = api_client
        self._seed(sink, repo)

        other = {"Authorization": "Bearer dev-tenant-b"}
        assert client.get("/api/v1/events", headers=other).json()["items"] == []
        assert client.get("/api/v1/events/pkg-evt-3", headers=other).status_code == 404
        assert client.get("/api/v1/cameras", headers=other).json()["cameras"] == []

        restricted = {"Authorization": "Bearer dev-site-a"}
        visible = client.get("/api/v1/events", headers=restricted).json()["items"]
        # cam-b has no site, so a site-restricted caller cannot see its history.
        assert [i["event_id"] for i in visible] == ["pkg-evt-3", "pkg-evt-2", "pkg-evt-0"]
        assert client.get("/api/v1/events/pkg-evt-1", headers=restricted).status_code == 404
        cameras = client.get("/api/v1/cameras", headers=restricted).json()["cameras"]
        assert [c["camera_id"] for c in cameras] == ["cam-a"]

    def test_redaction_never_leaks_model_ref_paths_or_payload(self, api_client):
        client, sink, repo = api_client
        self._seed(sink, repo)
        headers = {"Authorization": "Bearer dev-tenant-a"}

        page = client.get("/api/v1/events", headers=headers, params={"limit": 200})
        assert page.status_code == 200
        for item in page.json()["items"]:
            assert "model_ref" not in item
            assert "payload" not in item
            assert "frame_index" not in item
            evidence = item.get("evidence")
            assert evidence is None or set(evidence) == {"kind"}
            if item["event_id"] == "pkg-evt-0":
                # file_pts_only maps to null on the wire.
                assert item["time_basis"] is None

        detail = client.get("/api/v1/events/pkg-evt-3", headers=headers)
        assert detail.status_code == 200
        body = detail.json()
        assert "model_ref" not in body and "payload" not in body
        assert body["evidence"] == {"kind": "snapshot"}

        # Serialized bytes: no local path, model reference or payload value.
        for r in (page, detail):
            assert "models/yolo26n.pt" not in r.text
            assert "evidence/cam-01/fx.jpg" not in r.text
            assert "left_to_right" not in r.text

    def test_admin_grant_mutation_and_revocation(self, api_client):
        client, sink, repo = api_client
        self._seed(sink, repo)
        tenant_a = {"Authorization": "Bearer dev-tenant-a"}
        admin = {"Authorization": "Bearer dev-admin"}

        # Non-admin grant mutation is rejected.
        r = client.put(
            "/api/v1/grants/cam-a",
            headers=tenant_a,
            json={"site_id": "site_1", "tenant_ids": ["tenant_b"]},
        )
        assert r.status_code == 401

        # Admin replaces the assignment; tenant_a loses access immediately.
        r = client.put(
            "/api/v1/grants/cam-a",
            headers=admin,
            json={"site_id": "site_1", "tenant_ids": ["tenant_b"]},
        )
        assert r.status_code == 200
        assert r.json()["tenant_ids"] == ["tenant_b"]
        assert client.get("/api/v1/events/pkg-evt-3", headers=tenant_a).status_code == 404

        # Revocation removes the assignment entirely.
        r = client.delete("/api/v1/grants/cam-a", headers=admin)
        assert r.status_code == 200
        assert r.json()["tenant_ids"] == []
        other = {"Authorization": "Bearer dev-tenant-b"}
        assert client.get("/api/v1/events", headers=other).json()["items"] == []
