"""Grant-scoped reads, mutation retries and camera lifecycle API tests."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from ava.api.app import AppConfig, create_app
from ava.application.access import Camera, CameraGrant
from ava.application.mutation import (
    REPRESENTATION_VERSION,
    IdempotencyKeyReused,
    MutationResult,
    TargetState,
    make_etag,
)
from ava.events import EventRecord, EvidenceLocator
from ava.infrastructure.jwks_verifier import JwksRsaVerifier

ISSUER = "https://idp.ava.test"
AUDIENCE = "ava-api"
KID = "test-key-1"

CAM_A = "cam_a"
CAM_B = "cam_b"
SITE_A = "site_a"


class FakeGrantRepository:
    def __init__(
        self,
        grants: dict[str, CameraGrant],
        camera_repository: FakeCameraRepository | None = None,
    ) -> None:
        self._grants = dict(grants)
        self._camera_repo = camera_repository

    def get_camera_grant(self, camera_id: str) -> CameraGrant | None:
        return self._grants.get(camera_id)

    def get_camera_grant_with_revision(self, camera_id):
        grant = self.get_camera_grant(camera_id)
        cam = self._camera_repo.find_camera_by_camera_id(camera_id)
        return grant, cam.revision if cam else None

    def list_camera_grants(self) -> list[CameraGrant]:
        return sorted(self._grants.values(), key=lambda g: g.camera_id)

    def set_camera_grant(self, camera_id: str, site_id, tenant_ids) -> None:
        self._grants[camera_id] = CameraGrant(
            camera_id=camera_id, site_id=site_id, tenant_ids=frozenset(tenant_ids)
        )

    def revoke_camera_grant(self, camera_id: str) -> bool:
        return self._grants.pop(camera_id, None) is not None

    def set_camera_grant_and_reactivate_camera(
        self, camera_id: str, site_id, tenant_ids
    ) -> None:
        """Atomic grant + camera re-activation (in-memory: single dict update)."""
        if self._camera_repo is not None:
            self._camera_repo.upsert_camera(camera_id, site_id)
        self.set_camera_grant(camera_id, site_id, tenant_ids)


class FakeEventRepository:
    def __init__(self, records: list[EventRecord]) -> None:
        self._records = list(records)

    def find_by_event_id(self, event_id: str) -> EventRecord | None:
        for r in self._records:
            if r.event_id == event_id:
                return r
        return None

    def list_all(self) -> list[EventRecord]:
        return list(self._records)


class FakeCameraRepository:
    """In-memory camera registry with soft-delete semantics."""

    def __init__(self, cameras: dict[str, Camera]) -> None:
        self._cameras = dict(cameras)

    def get_camera(self, camera_id: str) -> Camera | None:
        cam = self._cameras.get(camera_id)
        if cam is None or cam.deleted_at is not None:
            return None
        return cam

    def list_cameras(self) -> list[Camera]:
        return sorted(
            (c for c in self._cameras.values() if c.deleted_at is None),
            key=lambda c: c.camera_id,
        )

    def upsert_camera(self, camera_id: str, site_id: str | None) -> None:
        existing = self._cameras.get(camera_id)
        if existing is not None:
            # Re-activate: clear deleted_at.
            self._cameras[camera_id] = Camera(
                camera_id=camera_id,
                site_id=site_id if site_id is not None else existing.site_id,
                deleted_at=None,
                updated_at=existing.updated_at,
            )
        else:
            self._cameras[camera_id] = Camera(
                camera_id=camera_id, site_id=site_id, deleted_at=None
            )

    def delete_camera(self, camera_id: str) -> bool:
        cam = self._cameras.get(camera_id)
        if cam is None or cam.deleted_at is not None:
            return False
        self._cameras[camera_id] = Camera(
            camera_id=camera_id,
            site_id=cam.site_id,
            deleted_at="2026-11-06T00:00:00Z",
            updated_at=cam.updated_at,
        )
        return True

    def find_camera_by_camera_id(self, camera_id: str) -> Camera | None:
        """Full row including soft-deleted; None when never created."""
        return self._cameras.get(camera_id)

    def get_revision(self, camera_id: str) -> int | None:
        """Current revision for optimistic concurrency."""
        cam = self._cameras.get(camera_id)
        return cam.revision if cam is not None else None

    def increment_revision(self, camera_id: str) -> None:
        """Increment the camera revision after a state change."""
        cam = self._cameras.get(camera_id)
        if cam is not None:
            self._cameras[camera_id] = Camera(
                camera_id=cam.camera_id,
                site_id=cam.site_id,
                deleted_at=cam.deleted_at,
                updated_at=cam.updated_at,
                revision=cam.revision + 1,
            )


class FakeMutationTransaction:
    """In-memory MutationTransaction for unit tests.

    Delegates to the fake grant and camera repositories. Idempotency is
    tracked in a simple dict keyed by (namespace, key_hash), storing the
    fingerprint alongside the response so that reuse with a different
    fingerprint raises IdempotencyKeyReused (matching production).
    """

    def __init__(
        self,
        grant_repo: FakeGrantRepository,
        camera_repo: FakeCameraRepository,
    ) -> None:
        self._grant_repo = grant_repo
        self._camera_repo = camera_repo
        # key: (namespace, key_hash) → (fingerprint, status, body, etag)
        # Fingerprint is recorded at claim time; response fields are filled
        # by store_completed before commit.
        self._store: dict[tuple[str, str], tuple[str, int | None, dict | None, str | None]] = {}
        self._namespace: str = ""

    def new_transaction(self, operation):
        child = FakeMutationTransaction(self._grant_repo, self._camera_repo)
        child._store = self._store
        child._operation = operation
        return child

    async def begin(self) -> None:
        pass

    async def claim_key(
        self, namespace: str, key_hash: str, fingerprint: str
    ) -> tuple[bool, MutationResult | None]:
        self._namespace = namespace
        k = (namespace, key_hash)
        if k in self._store:
            stored_fp, status, body, etag = self._store[k]
            if stored_fp != fingerprint:
                raise IdempotencyKeyReused(
                    "Idempotency key was already used for a different operation"
                )
            # Completed record → replay. (status is None only for pending,
            # which the production adapter treats as a fresh claim.)
            if status is not None:
                return (False, MutationResult(status_code=status, body=body, etag=etag, is_replay=True))
            # Pending with same fingerprint → treat as fresh claim.
            return (True, None)
        # New claim: record the fingerprint now; response filled later.
        self._store[k] = (fingerprint, None, None, None)
        return (True, None)

    async def lock_scope(self, scope) -> None:
        pass

    async def read_target(self, camera_id: str):
        cam = self._camera_repo.find_camera_by_camera_id(camera_id)
        if cam is None:
            return TargetState(exists=False)
        if self._operation == "camera_delete":
            return TargetState(True, cam.revision,
                               make_etag("camera", camera_id, REPRESENTATION_VERSION, cam.revision))
        grant = self._grant_repo.get_camera_grant(camera_id)
        etag = (
            make_etag("grant", camera_id, REPRESENTATION_VERSION, cam.revision)
            if grant is not None
            else None
        )
        data = {
            "camera_id": cam.camera_id,
            "name": None,
            "site_id": cam.site_id,
            "deleted_at": cam.deleted_at,
            "revision": cam.revision,
            "has_grant": grant is not None,
        }
        if grant is not None:
            data["tenant_ids"] = sorted(grant.tenant_ids)
        return TargetState(exists=True, revision=cam.revision, etag=etag, data=data)

    async def apply_mutation(self, camera_id: str, operation: str, params: dict):
        if operation == "grant_put":
            site_id = params.get("site_id")
            tenant_ids = params.get("tenant_ids", [])
            self._grant_repo.set_camera_grant_and_reactivate_camera(
                camera_id, site_id, tenant_ids
            )
            cam = self._camera_repo.find_camera_by_camera_id(camera_id)
            if cam is None:
                raise ValueError("camera_not_found")
            new_rev = cam.revision + 1
            self._camera_repo.increment_revision(camera_id)
            etag = make_etag("grant", camera_id, REPRESENTATION_VERSION, new_rev)
            data = {
                "camera_id": camera_id,
                "site_id": site_id if site_id is not None else cam.site_id,
                "tenant_ids": sorted(tenant_ids),
                "updated_at": None,
            }
            return TargetState(exists=True, revision=new_rev, etag=etag, data=data)

        if operation == "grant_delete":
            existed = self._grant_repo.revoke_camera_grant(camera_id)
            cam = self._camera_repo.find_camera_by_camera_id(camera_id)
            if cam is None:
                raise ValueError("camera_not_found")
            new_rev = cam.revision + 1 if existed else cam.revision
            if existed:
                self._camera_repo.increment_revision(camera_id)
            data = {
                "camera_id": camera_id,
                "site_id": cam.site_id,
                "deleted_at": cam.deleted_at,
                "revision": new_rev,
            }
            return TargetState(exists=True, revision=new_rev, etag=None, data=data)

        if operation == "camera_delete":
            cam = self._camera_repo.find_camera_by_camera_id(camera_id)
            if cam is None:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=404,
                    detail={"type": "about:blank", "title": "Not Found",
                            "status": 404, "code": "not_found",
                            "detail": "camera not found"},
                )
            if cam.deleted_at is not None:
                return TargetState(exists=True, revision=cam.revision, etag=None,
                                   data={"camera_id": camera_id, "revision": cam.revision})
            self._camera_repo.delete_camera(camera_id)
            new_rev = cam.revision + 1
            self._camera_repo.increment_revision(camera_id)
            return TargetState(exists=True, revision=new_rev, etag=None,
                               data={"camera_id": camera_id, "revision": new_rev})

        raise ValueError(f"unknown operation: {operation!r}")

    async def store_completed(
        self, key_hash: str, status_code: int, body: dict | None, etag: str | None
    ) -> None:
        k = (self._namespace, key_hash)
        fp = self._store[k][0]  # preserve fingerprint recorded at claim time
        self._store[k] = (fp, status_code, body, etag)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pending = [k for k, v in self._store.items() if v[1] is None]
        for key in pending:
            del self._store[key]


def _record(event_id: str, camera_id: str, emitted_utc: str) -> EventRecord:
    return EventRecord(
        schema_version="1",
        event_id=event_id,
        camera_id=camera_id,
        session_id=f"ses_{event_id}",
        track_id=1,
        rule_id="vehicle-zone",
        rule_type="zone_entry",
        class_label="car",
        confidence=0.9,
        source_time_s=1.0,
        emitted_utc=emitted_utc,
        config_version="1",
        model_ref=None,
        evidence=EvidenceLocator(kind="snapshot", path="/private/secret.mp4"),
    )


# Shared camera A granted to both tenants; site-less camera B likewise.
GRANTS = {
    CAM_A: CameraGrant(CAM_A, site_id=SITE_A, tenant_ids=frozenset({"ten_a", "ten_b"})),
    CAM_B: CameraGrant(CAM_B, site_id=None, tenant_ids=frozenset({"ten_a", "ten_b"})),
}

# Camera registry mirrors the grants: one row per camera.
CAMERAS = {
    CAM_A: Camera(CAM_A, site_id=SITE_A),
    CAM_B: Camera(CAM_B, site_id=None),
}

RECORDS = [
    _record("evt_003", CAM_A, "2026-11-05T10:00:03Z"),
    _record("evt_001", CAM_B, "2026-11-05T10:00:01Z"),
    _record("evt_002", CAM_A, "2026-11-05T10:00:02Z"),
]


def _make_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return _make_rsa_key()


@pytest.fixture(scope="module")
def jwk(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    pub = rsa_key.public_key().public_numbers()

    def _b64u(n: int, length: int) -> str:
        return base64_urlsafe(n.to_bytes(length, "big"))

    return {"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
            "n": _b64u(pub.n, 256), "e": _b64u(pub.e, 3)}


def base64_urlsafe(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_token(
    rsa_key: rsa.RSAPrivateKey,
    *,
    tenant: str = "ten_a",
    roles: list[str] | None = None,
    allowed_sites: list[str] | None = None,
) -> str:
    now = time.time()
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": f"user-{tenant}",
        "tenant": tenant,
        "roles": roles if roles is not None else ["viewer"],
        "exp": now + 3600.0,
        "iat": now,
    }
    if allowed_sites is not None:
        claims["allowed_sites"] = allowed_sites
    return pyjwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture()
def api_client(rsa_key, jwk) -> tuple[TestClient, dict[str, Any]]:
    client_http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"keys": [jwk]})
    ))
    policy = JwksRsaVerifier(
        issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE,
        client=client_http,
    )
    cam_repo = FakeCameraRepository(CAMERAS)
    grant_repo = FakeGrantRepository(GRANTS, camera_repository=cam_repo)
    from ava.application.mutation import MutationAdmission
    mutation_txn = FakeMutationTransaction(grant_repo, cam_repo)
    app = create_app(AppConfig(
        identity_policy=policy,
        grant_repository=grant_repo,
        event_repository=FakeEventRepository(RECORDS),
        camera_repository=cam_repo,
        mutation_txn=mutation_txn,
        mutation_admission=MutationAdmission(4),
    ))
    return TestClient(app), {"rsa_key": rsa_key}


def _auth(env: dict[str, Any], *, tenant: str = "ten_a",
          roles: list[str] | None = None,
          sites: list[str] | None = None,
          idem_key: str | None = None) -> dict[str, str]:
    token = _make_token(env["rsa_key"], tenant=tenant, roles=roles, allowed_sites=sites)
    if idem_key is None:
        # Deterministic per-test key: each call site that mutates state
        # must pass an explicit idem_key to avoid cross-operation replay.
        idem_key = _next_idem_key()
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": idem_key,
    }


# Deterministic idempotency key generator for tests. Each call yields a
# unique, stable UUID so that distinct operations get distinct keys while
# retrying the same operation can reuse an explicit key.
_idem_counter = 0


def _next_idem_key() -> str:
    global _idem_counter
    _idem_counter += 1
    return f"aaaaaaaa-0000-4000-8000-{_idem_counter:012d}"


class TestAuthAndScoping:
    def test_missing_token_401(self, api_client):
        client, _ = api_client
        r = client.get(
            "/api/v1/events",
            headers={"Idempotency-Key": "00000000-0000-4000-8000-000000000001"},
        )
        assert r.status_code == 401
        assert r.headers["content-type"].startswith("application/problem+json")

    def test_tenant_without_grant_sees_nothing(self, api_client):
        client, env = api_client
        r = client.get("/api/v1/events", headers=_auth(env, tenant="ten_c"))
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_site_restricted_caller_excludes_siteless_camera(self, api_client):
        client, env = api_client
        r = client.get(
            "/api/v1/events", headers=_auth(env, sites=[SITE_A])
        )
        ids = [i["event_id"] for i in r.json()["items"]]
        assert ids == ["evt_003", "evt_002"]  # CAM_B (site-less) excluded

    def test_ungranted_camera_events_invisible(self, api_client):
        client, env = api_client
        # evt records only reference granted cameras; drop a grant and re-check.
        repo: FakeGrantRepository = client.app.state.grant_repository
        repo._grants.pop(CAM_B)
        r = client.get("/api/v1/events", headers=_auth(env))
        ids = [i["event_id"] for i in r.json()["items"]]
        assert "evt_001" not in ids

    def test_cameras_inventory_scoped(self, api_client):
        client, env = api_client
        r = client.get("/api/v1/cameras", headers=_auth(env, sites=[SITE_A]))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert cams == [CAM_A]


class TestDetailAndCursor:
    def test_detail_visible(self, api_client):
        client, env = api_client
        r = client.get("/api/v1/events/evt_003", headers=_auth(env))
        assert r.status_code == 200
        body = r.json()
        assert body["event_id"] == "evt_003"
        assert body["camera_id"] == CAM_A

    def test_detail_missing_is_404(self, api_client):
        client, env = api_client
        r = client.get("/api/v1/events/evt_nope", headers=_auth(env))
        assert r.status_code == 404
        assert r.json()["code"] == "not_found"

    def test_detail_inaccessible_is_404(self, api_client):
        client, env = api_client
        # Site-restricted caller cannot see the site-less camera's event.
        r = client.get(
            "/api/v1/events/evt_001", headers=_auth(env, sites=[SITE_A])
        )
        assert r.status_code == 404

    def test_cursor_traversal_no_overlap(self, api_client):
        client, env = api_client
        seen: list[str] = []
        cursor = None
        for _ in range(5):
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            r = client.get("/api/v1/events", headers=_auth(env), params=params)
            assert r.status_code == 200
            body = r.json()
            seen.extend(i["event_id"] for i in body["items"])
            if body["next_cursor"] is None:
                break
            cursor = body["next_cursor"]
        assert seen == ["evt_003", "evt_002", "evt_001"]

    def test_invalid_cursor_is_400(self, api_client):
        client, env = api_client
        r = client.get(
            "/api/v1/events", headers=_auth(env), params={"cursor": "!!!bad!!!"}
        )
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_cursor"

    def test_page_size_bounds(self, api_client):
        client, env = api_client
        assert client.get(
            "/api/v1/events", headers=_auth(env), params={"limit": 0}
        ).status_code == 422
        assert client.get(
            "/api/v1/events", headers=_auth(env), params={"limit": 201}
        ).status_code == 422

    def test_response_has_request_id(self, api_client):
        client, env = api_client
        r = client.get("/api/v1/events", headers=_auth(env))
        assert "x-request-id" in {k.lower() for k in r.headers}


class TestRedaction:
    def test_no_paths_or_urls_in_event_payload(self, api_client):
        client, env = api_client
        r = client.get("/api/v1/events", headers=_auth(env))
        text = r.text
        assert "/private/secret.mp4" not in text
        assert "evidence_path" not in text
        # Evidence kind is exposed; path never is.
        assert '"kind":"snapshot"' in text.replace(" ", "")

    @pytest.mark.parametrize("model_ref", ["/private/model.pt", "model-name"])
    @pytest.mark.parametrize("endpoint", ["/api/v1/events", "/api/v1/events/evt_003"])
    def test_model_reference_is_omitted(self, api_client, model_ref, endpoint):
        client, env = api_client
        client.app.state.event_repository = FakeEventRepository(
            [replace(RECORDS[0], model_ref=model_ref)]
        )

        response = client.get(endpoint, headers=_auth(env))

        assert response.status_code == 200
        item = response.json()["items"][0] if endpoint.endswith("events") else response.json()
        assert "model_ref" not in item
        assert model_ref not in response.text

    def test_no_tenant_field_on_events(self, api_client):
        client, env = api_client
        r = client.get("/api/v1/events", headers=_auth(env))
        item = r.json()["items"][0]
        assert "tenant" not in item and "tenant_id" not in item

    def test_unusable_grant_data_is_502(self, api_client):
        client, env = api_client

        class Broken(FakeGrantRepository):
            def list_camera_grants(self):
                from ava.application.access import GrantLookupError

                raise GrantLookupError("cam_x", "malformed row")

        client.app.state.grant_repository = Broken(GRANTS)
        r = client.get("/api/v1/events", headers=_auth(env))
        assert r.status_code == 502
        assert r.json()["code"] == "grant_data_unusable"


class TestGrantMutation:
    def test_viewer_cannot_mutate_grants(self, api_client):
        client, env = api_client
        r = client.put(
            f"/api/v1/grants/{CAM_A}",
            headers=_auth(env),
            json={"site_id": SITE_A, "tenant_ids": ["ten_b"]},
        )
        assert r.status_code == 401

    def test_admin_can_replace_and_revoke(self, api_client):
        client, env = api_client
        admin_put = _auth(env, roles=["admin"], idem_key="bbbbbbbb-0000-4000-8000-000000000001")
        r = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin_put,
            json={"site_id": SITE_A, "tenant_ids": ["ten_b"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["camera_id"] == CAM_A
        assert body["tenant_ids"] == ["ten_b"]

        # ten_a lost access immediately (current grants govern reads, D7).
        r = client.get("/api/v1/events", headers=_auth(env, tenant="ten_a"))
        ids = [i["event_id"] for i in r.json()["items"]]
        assert "evt_003" not in ids and "evt_002" not in ids

        # ten_b still sees the shared camera history.
        r = client.get("/api/v1/events", headers=_auth(env, tenant="ten_b"))
        ids = [i["event_id"] for i in r.json()["items"]]
        assert "evt_003" in ids and "evt_002" in ids

        # Revoke: camera disappears from inventory and history.
        admin_del = _auth(env, roles=["admin"], idem_key="bbbbbbbb-0000-4000-8000-000000000002")
        r = client.delete(f"/api/v1/grants/{CAM_A}", headers=admin_del)
        assert r.status_code == 204
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A not in cams

    def test_put_then_delete_same_key_returns_409(self, api_client):
        """PUT → DELETE with the same idempotency key must return 409.

        The DELETE has a different fingerprint (different method and path),
        so the stored PUT result cannot be replayed. The grant is NOT deleted.
        """
        client, env = api_client
        shared_key = "cccccccc-0000-4000-8000-000000000001"
        admin_put = _auth(env, roles=["admin"], idem_key=shared_key)
        r = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin_put,
            json={"site_id": SITE_A, "tenant_ids": ["ten_b"]},
        )
        assert r.status_code == 200

        # DELETE with the same key but different fingerprint → 409.
        admin_del = _auth(env, roles=["admin"], idem_key=shared_key)
        r = client.delete(f"/api/v1/grants/{CAM_A}", headers=admin_del)
        assert r.status_code == 409
        assert r.json()["code"] == "idempotency_key_reused"

        # The grant was NOT deleted — it still exists.
        admin_get = _auth(env, roles=["admin"])
        r = client.get(f"/api/v1/grants/{CAM_A}", headers=admin_get)
        assert r.status_code == 200
        assert r.json()["camera_id"] == CAM_A

    def test_identical_put_retry_replays_original_result(self, api_client):
        """An identical PUT retry (same key, same fingerprint) replays the
        original 200 result without re-applying the mutation."""
        client, env = api_client
        shared_key = "dddddddd-0000-4000-8000-000000000001"
        admin_put = _auth(env, roles=["admin"], idem_key=shared_key)


        r1 = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin_put,
            json={"site_id": SITE_A, "tenant_ids": ["ten_b"]},
        )
        assert r1.status_code == 200
        body1 = r1.json()

        # Identical retry: same key, same method, same path, same body.
        admin_put_retry = _auth(env, roles=["admin"], idem_key=shared_key)
        r2 = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin_put_retry,
            json={"site_id": SITE_A, "tenant_ids": ["ten_b"]},
        )
        assert r2.status_code == 200
        body2 = r2.json()
        # Replay returns the original result.
        assert body1 == body2


class TestGrantRequestValidation:
    def test_empty_tenant_ids_rejected_422(self, api_client):
        """PUT /grants/{cam} with an empty tenant_ids list must return 422."""
        client, env = api_client
        admin = _auth(env, roles=["admin"], idem_key="ffffffff-0000-4000-8000-000000000001")
        r = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin,
            json={"site_id": SITE_A, "tenant_ids": []},
        )
        assert r.status_code == 422


class TestCameraSoftDelete:
    def test_viewer_cannot_delete_camera(self, api_client):
        """Non-admin callers cannot soft-delete a camera."""
        client, env = api_client
        r = client.delete(f"/api/v1/cameras/{CAM_A}", headers=_auth(env))
        assert r.status_code == 401

    def test_admin_soft_delete_sets_deleted_at(self, api_client):
        """DELETE /cameras/{id} sets deleted_at on the camera row."""
        client, env = api_client
        admin = _auth(env, roles=["admin"], idem_key="eeeeeeee-0000-4000-8000-000000000001")
        r = client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin)
        assert r.status_code == 204

        # The row is retained with deleted_at set.
        repo: FakeCameraRepository = client.app.state.camera_repository
        cam = repo.find_camera_by_camera_id(CAM_A)
        assert cam is not None
        assert cam.deleted_at is not None

    def test_soft_deleted_camera_invisible_to_inventory(self, api_client):
        """A soft-deleted camera does not appear in GET /cameras."""
        client, env = api_client
        admin = _auth(env, roles=["admin"], idem_key="eeeeeeee-0000-4000-8000-000000000002")
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin)

        # ten_b still has a grant to CAM_A but the camera is deleted.
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A not in cams

    def test_soft_deleted_camera_events_still_accessible(self, api_client):
        """Events of a soft-deleted camera remain accessible to granted tenants.

        The camera row is retained, so event-to-camera mapping resolves and
        the grant still governs access. This is the core rationale for
        soft-delete on the camera entity.
        """
        client, env = api_client
        admin = _auth(env, roles=["admin"], idem_key="eeeeeeee-0000-4000-8000-000000000003")
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin)

        # ten_a still has a grant to CAM_A; events remain visible.
        r = client.get("/api/v1/events", headers=_auth(env, tenant="ten_a"))
        ids = [i["event_id"] for i in r.json()["items"]]
        assert "evt_003" in ids and "evt_002" in ids

    def test_reactivation_via_grant_upsert(self, api_client):
        """PUT /grants/{id} re-activates a soft-deleted camera."""
        client, env = api_client
        admin_del = _auth(env, roles=["admin"], idem_key="eeeeeeee-0000-4000-8000-000000000004")
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin_del)

        # Confirm it's gone from inventory.
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A not in cams

        # Re-activate by upserting a grant.
        admin_put = _auth(env, roles=["admin"], idem_key="eeeeeeee-0000-4000-8000-000000000005")
        r = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin_put,
            json={"site_id": SITE_A, "tenant_ids": ["ten_a", "ten_b"]},
        )
        assert r.status_code == 200

        # Camera is back in inventory.
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A in cams


class TestCameraStateEndpoint:
    def test_viewer_cannot_inspect_camera_state(self, api_client):
        """Non-admin callers cannot inspect camera state."""
        client, env = api_client
        r = client.get(f"/api/v1/cameras/{CAM_A}", headers=_auth(env))
        assert r.status_code == 401

    def test_admin_can_inspect_active_camera(self, api_client):
        """GET /cameras/{id} returns the full row for an active camera."""
        client, env = api_client
        admin = _auth(env, roles=["admin"])
        r = client.get(f"/api/v1/cameras/{CAM_A}", headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert body["camera_id"] == CAM_A
        assert body["site_id"] == SITE_A
        assert body["deleted_at"] is None

    def test_admin_can_inspect_soft_deleted_camera(self, api_client):
        """GET /cameras/{id} returns the row with deleted_at for a deleted camera."""
        client, env = api_client
        admin_del = _auth(env, roles=["admin"], idem_key="eeeeeeee-0000-4000-8000-000000000006")
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin_del)

        admin = _auth(env, roles=["admin"])
        r = client.get(f"/api/v1/cameras/{CAM_A}", headers=admin)
        assert r.status_code == 200
        body = r.json()
        assert body["camera_id"] == CAM_A
        assert body["deleted_at"] is not None

    def test_missing_camera_is_404(self, api_client):
        """GET /cameras/{id} returns 404 when no row exists."""
        client, env = api_client
        admin = _auth(env, roles=["admin"])
        r = client.get("/api/v1/cameras/cam_never_created", headers=admin)
        assert r.status_code == 404
        assert r.json()["code"] == "not_found"
