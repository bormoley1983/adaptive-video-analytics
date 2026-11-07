"""Installed HTTP API, real identities and an approved event fixture."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

import pytest
from alembic.config import Config
from api_acceptance_support import (
    APPROVED_EVENTS_SHA256,
    ROOT,
    SITE,
    IdentityStack,
    load_approved_events,
    running_api,
)
from api_http_bootstrap import installed_api_provenance

from alembic import command
from ava.events import EventRecord, EvidenceLocator
from ava.persistence.postgres import PostgresEventSink

pytestmark = pytest.mark.e2e


def headers(token, correlation=None):
    return {"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid.uuid4()),
            "X-Correlation-Id": correlation or str(uuid.uuid4())}


@pytest.fixture(scope="module")
def api():
    runner_proof = installed_api_provenance()
    for name, digest in runner_proof["module_sha256"].items():
        relative = "ava/__init__.py" if name == "ava" else name.replace(".", "/") + ".py"
        assert hashlib.sha256((ROOT / "src" / relative).read_bytes()).hexdigest() == digest
    records = load_approved_events(ROOT / "tests/fixtures/f1_s6_real_events_sanitized.json")
    with IdentityStack() as identity:
        dsn = identity.event_database()
        # Alembic's environment reads the DSN; never inherit a developer database or schema.
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("AVA_PG_DSN", dsn)
            patch.delenv("PGOPTIONS", raising=False)
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "alembic"))
            config.set_main_option("prepend_sys_path", "")
            command.upgrade(config, "head")
            sink = PostgresEventSink(dsn)
            try:
                sink.check_schema()
                for data in records:
                    assert sink.write(EventRecord(**{**data, "evidence": EvidenceLocator(**data["evidence"])}))
            finally:
                sink.close()
            evidence = identity.evidence_root / identity.name
            evidence.mkdir(parents=True, exist_ok=True)
            runner_proof.update(fixture_sha256=APPROVED_EVENTS_SHA256, seeded_records=len(records))
            (evidence / "runner-package.json").write_text(json.dumps(runner_proof, indent=2))
            with running_api(identity, dsn) as (client, proof_path):
                proof = json.loads(proof_path.read_text())
                assert proof["noneditable"] and proof["startup_complete"]
                assert proof["module_sha256"] == runner_proof["module_sha256"]
                yield client, identity, records


@pytest.fixture
def assigned(api):
    client, identity, records = api
    admin = identity.service_token()
    camera = records[0]["camera_id"]
    response = client.put(f"/api/v1/grants/{camera}", headers=headers(admin),
                          json={"site_id": SITE, "tenant_ids": ["tenant-a"]})
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"')
    try:
        yield client, identity, records, admin
    finally:
        response = client.delete(f"/api/v1/grants/{camera}", headers=headers(admin))
        assert response.status_code == 204


def test_approved_events_are_retrieved_over_installed_http_api(assigned):
    client, identity, records, admin = assigned
    viewer = identity.human_token()
    correlation = str(uuid.uuid4())
    expected = {record["event_id"]: record for record in records}
    observed = []
    cursors = set()
    cursor = None
    for _ in range(len(records) + 1):
        params = {"limit": 5}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/api/v1/events", headers=headers(viewer, correlation), params=params)
        assert response.status_code == 200
        assert response.headers["X-Correlation-Id"] == correlation
        uuid.UUID(response.headers["X-Request-Id"])
        body = response.json()
        observed.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert cursor not in cursors
        cursors.add(cursor)
    else:
        pytest.fail("Event cursor did not terminate")

    assert len(cursors) == 2
    assert [item["event_id"] for item in observed] == [
        row["event_id"] for row in sorted(records, key=lambda row: (
            datetime.fromisoformat(row["emitted_utc"]), row["event_id"]), reverse=True)
    ]
    for item in observed:
        original = expected[item["event_id"]]
        for name in ("camera_id", "session_id", "track_id", "rule_id", "rule_type",
                     "class_label", "confidence", "source_time_s", "schema_version", "config_version"):
            assert item[name] == original[name]
        assert datetime.fromisoformat(item["emitted_utc"]) == datetime.fromisoformat(original["emitted_utc"])
        kind = original["evidence"]["kind"]
        assert item["evidence"] == ({"kind": kind} if kind is not None else None)
        assert not {"path", "payload", "model_ref", "frame_index", "tenant_id", "tenant"}.intersection(item)
        if item["evidence"] is not None:
            assert "path" not in item["evidence"]
        assert item["capture_utc"] is None and item["time_basis"] is None
    selected = records[0]["event_id"]
    detail = client.get(f"/api/v1/events/{selected}", headers=headers(viewer))
    service_detail = client.get(f"/api/v1/events/{selected}", headers=headers(admin))
    assert detail.status_code == service_detail.status_code == 200
    assert detail.json() == service_detail.json() == next(
        item for item in observed if item["event_id"] == selected
    )


@pytest.mark.parametrize("username", ["other-tenant", "other-site"])
def test_real_identity_without_camera_scope_cannot_discover_events(assigned, username):
    client, identity, records, _ = assigned
    token = identity.human_token(username)
    allowed = client.get(f"/api/v1/events/{records[0]['event_id']}",
                         headers=headers(identity.human_token()))
    assert allowed.status_code == 200

    detail = client.get(f"/api/v1/events/{records[0]['event_id']}", headers=headers(token))
    collection = client.get("/api/v1/events", headers=headers(token))

    assert detail.status_code == 404
    assert detail.json()["code"] == "not_found"
    assert collection.status_code == 200
    assert collection.json() == {"items": [], "next_cursor": None}


def test_revocation_hides_history_and_regrant_restores_it(assigned):
    client, identity, records, admin = assigned
    viewer = identity.human_token()
    camera = records[0]["camera_id"]
    path = f"/api/v1/events/{records[0]['event_id']}"
    before = client.get(path, headers=headers(viewer))
    assert before.status_code == 200

    removed = client.delete(f"/api/v1/grants/{camera}", headers=headers(admin))
    after = client.get(path, headers=headers(viewer))
    collection = client.get("/api/v1/events", headers=headers(viewer))

    assert removed.status_code == 204 and removed.content == b""
    assert "ETag" not in removed.headers
    assert after.status_code == 404
    assert collection.json() == {"items": [], "next_cursor": None}
    restored = client.put(f"/api/v1/grants/{camera}", headers=headers(admin),
                          json={"site_id": SITE, "tenant_ids": ["tenant-a"]})
    assert restored.status_code == 200
    assert client.get(path, headers=headers(viewer)).json() == before.json()


def test_human_viewer_cannot_replace_the_trusted_grant(assigned):
    client, identity, records, _ = assigned
    viewer = identity.human_token()
    camera = records[0]["camera_id"]

    denied = client.put(f"/api/v1/grants/{camera}", headers=headers(viewer),
                        json={"site_id": SITE, "tenant_ids": ["tenant-b"]})

    assert denied.status_code == 401
    assert denied.json()["code"] == "insufficient_role"
    assert client.get(f"/api/v1/events/{records[0]['event_id']}", headers=headers(viewer)).status_code == 200
