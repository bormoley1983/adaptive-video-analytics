"""PostgreSQL pool and request-local mutation transactions."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from ..application.mutation import (
    REPRESENTATION_VERSION,
    IdempotencyKeyReused,
    MutationResult,
    MutationScope,
    RequestInProgress,
    TargetNotFound,
    TargetState,
    make_etag,
)


def scope_lock_key(scope: MutationScope) -> tuple[int, int]:
    digest = hashlib.sha256(scope.key.encode()).digest()
    return (int.from_bytes(digest[:4], "big", signed=True),
            int.from_bytes(digest[4:8], "big", signed=True))


def lock_camera(conn, camera_id: str) -> None:
    """Serialize non-HTTP writers with API mutations."""
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                 scope_lock_key(MutationScope(f"camera:{camera_id}")))


class PostgresMutationTransaction:
    """Own a shared pool; create independent transactions for each request."""

    def __init__(self, dsn: str, *, pool_min: int = 1, pool_max: int = 4) -> None:
        self.pool_max = pool_max
        self._pool = AsyncConnectionPool(dsn, min_size=pool_min, max_size=pool_max,
                                         name="ava-mutation", open=False)

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    def new_transaction(self, operation: str):
        return _MutationTransaction(self._pool, operation)


class _MutationTransaction:
    def __init__(self, pool, operation: str) -> None:
        self._pool = pool
        self._operation = operation
        self._conn = None

    async def begin(self) -> None:
        self._conn = await self._pool.getconn()
        try:
            await self._conn.execute("SET LOCAL lock_timeout = '5s'")
        except BaseException:
            await self.rollback()
            raise

    async def _execute(self, sql: str, params=()):
        try:
            return await self._conn.execute(sql, params)
        except psycopg.errors.LockNotAvailable as exc:
            raise RequestInProgress("mutation lock timeout") from exc

    async def claim_key(self, namespace: str, key_hash: str, fingerprint: str):
        self._namespace = namespace
        cur = await self._execute(
            "INSERT INTO idempotency_keys (key_hash, namespace, fingerprint, status, "
            "created_at, expires_at) VALUES (%s, %s, %s, 'pending', clock_timestamp(), "
            "clock_timestamp() + interval '1 hour') "
            "ON CONFLICT (key_hash) DO UPDATE SET namespace = EXCLUDED.namespace, "
            "fingerprint = EXCLUDED.fingerprint, status = 'pending', response_status = NULL, "
            "response_body = NULL, etag = NULL, created_at = EXCLUDED.created_at, "
            "expires_at = EXCLUDED.expires_at "
            "WHERE idempotency_keys.expires_at <= clock_timestamp() RETURNING key_hash",
            (key_hash, namespace, fingerprint),
        )
        if await cur.fetchone() is not None:
            return True, None
        cur = await self._execute(
            "SELECT fingerprint, status, response_status, response_body, etag "
            "FROM idempotency_keys WHERE key_hash = %s AND namespace = %s",
            (key_hash, namespace),
        )
        row = await cur.fetchone()
        if row is None:
            raise RequestInProgress("claim ownership unavailable")
        stored_fp, status, response_status, body, etag = row
        if stored_fp != fingerprint:
            raise IdempotencyKeyReused()
        if status != "completed":
            raise RequestInProgress("unfinished claim")
        return False, MutationResult(response_status, body, etag, is_replay=True)

    async def lock_scope(self, scope: MutationScope) -> None:
        await self._execute("SELECT pg_advisory_xact_lock(%s, %s)", scope_lock_key(scope))

    async def read_target(self, camera_id: str) -> TargetState:
        if self._operation.startswith("grant_"):
            cur = await self._execute(
                "SELECT g.site_id, g.tenant_ids, g.updated_at, c.revision "
                "FROM camera_grants g JOIN cameras c USING (camera_id) WHERE camera_id = %s",
                (camera_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return TargetState(False)
            site, tenants, updated, revision = row
            data = {"camera_id": camera_id, "site_id": site,
                    "tenant_ids": sorted(tenants), "updated_at": updated.isoformat()}
            kind = "grant"
        else:
            cur = await self._execute(
                "SELECT site_id, deleted_at, updated_at, revision FROM cameras WHERE camera_id = %s",
                (camera_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise TargetNotFound(camera_id)
            site, deleted, updated, revision = row
            data = {"camera_id": camera_id, "site_id": site,
                    "deleted_at": deleted.isoformat() if deleted else None,
                    "updated_at": updated.isoformat()}
            kind = "camera"
        return TargetState(True, revision,
                           make_etag(kind, camera_id, REPRESENTATION_VERSION, revision), data)

    async def apply_mutation(self, camera_id: str, operation: str, params: dict[str, Any]):
        if operation == "grant_put":
            await self._execute(
                "INSERT INTO cameras (camera_id, site_id) VALUES (%s, %s) "
                "ON CONFLICT (camera_id) DO UPDATE SET site_id = EXCLUDED.site_id, "
                "deleted_at = NULL, updated_at = now(), revision = cameras.revision + 1",
                (camera_id, params.get("site_id")),
            )
            await self._execute(
                "INSERT INTO camera_grants (camera_id, site_id, tenant_ids) VALUES (%s, %s, %s) "
                "ON CONFLICT (camera_id) DO UPDATE SET site_id = EXCLUDED.site_id, "
                "tenant_ids = EXCLUDED.tenant_ids, updated_at = now()",
                (camera_id, params.get("site_id"), sorted(set(params["tenant_ids"]))),
            )
        elif operation == "grant_delete":
            cur = await self._execute("DELETE FROM camera_grants WHERE camera_id = %s",
                                      (camera_id,))
            if cur.rowcount:
                await self._execute("UPDATE cameras SET revision = revision + 1, "
                                    "updated_at = now() WHERE camera_id = %s", (camera_id,))
            return TargetState(False)
        elif operation == "camera_delete":
            await self._execute(
                "UPDATE cameras SET deleted_at = now(), updated_at = now(), revision = revision + 1 "
                "WHERE camera_id = %s AND deleted_at IS NULL", (camera_id,))
        else:
            raise ValueError(f"unknown mutation: {operation}")
        return await self.read_target(camera_id)

    async def store_completed(self, key_hash: str, status_code: int, body, etag) -> None:
        cur = await self._execute(
            "UPDATE idempotency_keys SET status = 'completed', response_status = %s, "
            "response_body = %s::jsonb, etag = %s, "
            "expires_at = clock_timestamp() + interval '1 hour' "
            "WHERE key_hash = %s AND namespace = %s AND status = 'pending'",
            (status_code, json.dumps(body), etag, key_hash, self._namespace),
        )
        if cur.rowcount != 1:
            raise RuntimeError("mutation lost its idempotency claim")

    async def commit(self) -> None:
        try:
            await self._conn.commit()
        finally:
            await self._release()

    async def rollback(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.rollback()
            finally:
                await self._release()

    async def _release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await self._pool.putconn(conn)
