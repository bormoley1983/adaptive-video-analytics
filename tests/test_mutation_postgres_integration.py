"""HTTP mutation guarantees against disposable PostgreSQL."""
from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql

from alembic import command
from ava.api.app import compose_postgres_app
from ava.application.identity import CallerIdentity
from ava.application.mutation import MutationScope, execute_idempotent_mutation
from ava.persistence.mutation_postgres import PostgresMutationTransaction, lock_camera

pytestmark = pytest.mark.integration
BODY = {"site_id": "site-a", "tenant_ids": ["tenant-a"]}


class Policy:
    def verify(self, token):
        return CallerIdentity(token, token, frozenset({"viewer"}) if token == "viewer" else frozenset({"admin"}))


def headers(key=None, caller="admin", **extra):
    return {"Authorization": f"Bearer {caller}", "Idempotency-Key": key or str(uuid.uuid4()), **extra}


@pytest.fixture
def database(pg_container, monkeypatch):
    dsn = pg_container["dsn"]
    schema = "mut_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    monkeypatch.setenv("AVA_PG_DSN", dsn)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def api(database):
    app = compose_postgres_app(database, identity_policy=Policy())
    with TestClient(app) as client:
        yield client


def put(client, camera="one", key=None, caller="admin", body=None, **extra):
    return client.put(f"/api/v1/grants/{camera}", headers=headers(key, caller, **extra),
                      json=BODY if body is None else body)


def code(response):
    return response.json()["code"]


def test_replay_precedes_stale_precondition_and_preserves_exact_body(api):
    first = put(api)
    etag = first.headers["ETag"]
    key = str(uuid.uuid4())
    success = put(api, key=key, **{"If-Match": etag})
    assert success.status_code == 200
    assert put(api, body={"site_id": None, "tenant_ids": ["other"]}).status_code == 200
    replay = put(api, key=key, **{"If-Match": etag})
    assert replay.status_code == 200
    assert replay.json() == success.json()
    assert replay.headers["ETag"] == success.headers["ETag"]
    assert replay.headers["X-Request-Id"] != success.headers["X-Request-Id"]
    assert put(api, key=key, **{"If-Match": '"changed"'}).status_code == 409


def test_key_reuse_across_method_path_and_body_conflicts(api):
    key = str(uuid.uuid4())
    assert put(api, key=key).status_code == 200
    responses = [api.delete("/api/v1/grants/one", headers=headers(key)),
                 put(api, "two", key=key),
                 put(api, key=key, body={"tenant_ids": ["other"]})]
    assert all(r.status_code == 409 and code(r) == "idempotency_key_reused" for r in responses)
    assert api.get("/api/v1/grants/one", headers=headers()).status_code == 200


def test_same_uuid_under_different_callers_has_independent_records(api, database):
    key = str(uuid.uuid4())
    assert put(api, "one", key=key, caller="alice").status_code == 200
    assert put(api, "two", key=key, caller="bob").status_code == 200
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0] == 2


def test_expired_key_is_replaced_atomically(api, database):
    key = str(uuid.uuid4())
    original = put(api, key=key)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE idempotency_keys SET expires_at = clock_timestamp() - interval '1 second'")
    changed = put(api, key=key, body={"tenant_ids": ["new"]})
    assert changed.status_code == 200
    assert changed.headers["ETag"] != original.headers["ETag"]
    assert changed.json()["tenant_ids"] == ["new"]
    assert put(api, key=key, body={"tenant_ids": ["new"]}).json() == changed.json()


def test_camera_delete_uses_camera_validator_and_preserves_grants(api, database):
    grant = put(api)
    camera = api.get("/api/v1/cameras/one", headers=headers())
    assert camera.headers["ETag"] != grant.headers["ETag"]
    wrong = api.delete("/api/v1/cameras/one", headers=headers(**{"If-Match": grant.headers["ETag"]}))
    assert wrong.status_code == 412
    result = api.delete("/api/v1/cameras/one", headers=headers(**{"If-Match": camera.headers["ETag"]}))
    assert result.status_code == 204 and "ETag" not in result.headers
    assert api.get("/api/v1/grants/one", headers=headers()).status_code == 200
    with psycopg.connect(database) as conn:
        revision = conn.execute("SELECT revision FROM cameras WHERE camera_id='one'").fetchone()[0]
    assert api.delete("/api/v1/cameras/one", headers=headers()).status_code == 204
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT revision FROM cameras WHERE camera_id='one'").fetchone()[0] == revision
    assert put(api).status_code == 200
    assert api.get("/api/v1/cameras/one", headers=headers()).json()["deleted_at"] is None


def test_grant_get_and_put_return_same_representation_and_validator(api):
    write = put(api, body={"tenant_ids": ["z", "a", "a"], "site_id": None})
    read = api.get("/api/v1/grants/one", headers=headers())
    assert write.json() == read.json()
    assert write.headers["ETag"] == read.headers["ETag"]
    assert read.json()["tenant_ids"] == ["a", "z"]


@pytest.mark.parametrize("target", ["grant", "camera"])
def test_conditional_deletes_check_current_target(api, target):
    put(api)
    path = f"/api/v1/{target}s/one"
    current = api.get(path, headers=headers()).headers["ETag"]
    assert api.delete(path, headers=headers(**{"If-Match": '"stale"'})).status_code == 412
    assert api.delete(path, headers=headers(**{"If-Match": f'W/{current}, "other", {current}'})).status_code == 204


def test_absent_targets_obey_preconditions_and_normal_error_precedence(api):
    assert api.delete("/api/v1/grants/missing", headers=headers()).status_code == 204
    assert api.delete("/api/v1/grants/missing", headers=headers(**{"If-Match": "*"})).status_code == 412
    assert put(api, **{"If-Match": "*"}).status_code == 412
    assert api.delete("/api/v1/cameras/missing", headers=headers(**{"If-Match": "*"})).status_code == 404
    assert put(api).status_code == 200
    assert api.delete("/api/v1/grants/one", headers=headers(**{"If-Match": "*"})).status_code == 204
    assert api.get("/api/v1/grants/one", headers=headers()).status_code == 404


def test_current_authorization_is_checked_before_replay(api):
    key = str(uuid.uuid4())
    assert put(api, key=key).status_code == 200
    api.app.state.identity_policy = type("Revoked", (), {
        "verify": lambda self, token: CallerIdentity("admin", "admin", frozenset({"viewer"}))})()
    assert put(api, key=key).status_code == 401


def test_concurrent_identical_requests_mutate_once(api, database):
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: put(api, key=key), range(4)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.headers["ETag"] for r in responses}) == 1
    assert all(r.json() == responses[0].json() for r in responses)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT revision FROM cameras WHERE camera_id='one'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0] == 1


def test_two_conditional_writers_cannot_both_succeed(api):
    old = put(api).headers["ETag"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: put(api, **{"If-Match": old}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 412]


def test_failed_mutation_rolls_back_claim_and_can_retry(api, database):
    key = str(uuid.uuid4())
    with psycopg.connect(database) as conn:
        conn.execute("CREATE FUNCTION fail_write() RETURNS trigger LANGUAGE plpgsql AS $$ "
                     "BEGIN RAISE EXCEPTION 'injected failure'; END $$")
        conn.execute("CREATE TRIGGER fail_grant BEFORE INSERT ON camera_grants "
                     "FOR EACH ROW EXECUTE FUNCTION fail_write()")
    failure = put(api, key=key)
    assert failure.status_code == 500
    assert failure.json()["request_id"] == failure.headers["X-Request-Id"]
    assert failure.headers["X-Correlation-Id"]
    assert "injected" not in failure.text
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM cameras").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_grant ON camera_grants")
    assert put(api, key=key).status_code == 200


def test_completed_records_require_response_status(database):
    with psycopg.connect(database) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO idempotency_keys (key_hash, namespace, fingerprint, status, expires_at) "
                     "VALUES ('key','caller','fp','completed', now())")


def wait_for_lock_waiter(dsn):
    deadline = time.monotonic() + 3
    with psycopg.connect(dsn, autocommit=True) as conn:
        while time.monotonic() < deadline:
            if conn.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                            "AND query LIKE 'SELECT pg_advisory_xact_lock%%'").fetchone()[0]:
                return
            time.sleep(0.01)
    pytest.fail("request never reached the database lock")


def test_duplicate_wait_does_not_block_query_or_exceed_admission(api, database):
    assert put(api).status_code == 200
    key = str(uuid.uuid4())
    with psycopg.connect(database) as blocker, ThreadPoolExecutor(max_workers=5) as executor:
        lock_camera(blocker, "one")
        futures = [executor.submit(put, api, key=key) for _ in range(4)]
        try:
            wait_for_lock_waiter(database)
            deadline = time.monotonic() + 2
            while api.app.state.mutation_admission.active != 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            started = time.monotonic()
            unrelated = api.get("/api/v1/grants/one", headers=headers())
            assert unrelated.status_code == 200
            assert time.monotonic() - started < 1
            rejected = put(api, "other")
            assert rejected.status_code == 409 and code(rejected) == "request_in_progress"
        finally:
            blocker.rollback()
        responses = [future.result(timeout=5) for future in futures]
    assert all(r.status_code == 200 for r in responses)
    assert api.app.state.mutation_admission.active == 0
    assert put(api, "other").status_code == 200


def test_scope_lock_timeout_returns_409_and_releases_claim(api, database):
    key = str(uuid.uuid4())
    with psycopg.connect(database) as blocker:
        lock_camera(blocker, "one")
        started = time.monotonic()
        result = put(api, key=key)
        assert 4.5 <= time.monotonic() - started < 8
        assert result.status_code == 409 and code(result) == "request_in_progress"
        blocker.rollback()
    assert put(api, key=key).status_code == 200


def test_adapter_absence_rejects_writes(api):
    api.app.state.mutation_txn = None
    assert put(api).status_code == 502
    assert api.app.state.camera_repository.find_camera_by_camera_id("one") is None


def test_cancellation_releases_real_claim_and_connection(database):
    async def scenario():
        factory = PostgresMutationTransaction(database, pool_max=2)
        await factory.open()
        owner = factory.new_transaction("grant_put")
        await owner.begin()
        await owner.lock_scope(MutationScope("camera:one"))
        waiting = factory.new_transaction("grant_put")
        entered = asyncio.Event()
        original = waiting.lock_scope
        async def enter(scope):
            entered.set()
            await original(scope)
        waiting.lock_scope = enter
        try:
            task = asyncio.create_task(execute_idempotent_mutation(
                txn=waiting, namespace="caller", key_hash="key", fingerprint="fp",
                scope=MutationScope("camera:one"), camera_id="one", operation="grant_put", params=BODY))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await owner.rollback()
            retry = factory.new_transaction("grant_put")
            result = await execute_idempotent_mutation(
                txn=retry, namespace="caller", key_hash="key", fingerprint="fp",
                scope=MutationScope("camera:one"), camera_id="one", operation="grant_put", params=BODY)
            assert result.status_code == 200
        finally:
            await owner.rollback()
            await factory.close()
    asyncio.run(scenario())
