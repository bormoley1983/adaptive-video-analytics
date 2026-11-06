"""API tests for the S5 read slice and F-1-S6 camera soft-delete.

Deterministic: in-memory grant/event/camera fakes plus real RS256 tokens via
the JwksRsaVerifier pattern. Covers authentication, tenant/camera/site
scoping, 404 for inaccessible objects, 400 for invalid cursors, cursor
traversal, evidence redaction, admin-only grant mutation (decisions D3-D8),
grant request validation (G1), and camera soft-delete with event-to-camera
mapping preservation (F-1-S6, ADR-19).
"""

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
from ava.events import EventRecord, EvidenceLocator
from ava.infrastructure.jwks_verifier import JwksRsaVerifier

ISSUER = "https://idp.ava.test"
AUDIENCE = "ava-api"
KID = "test-key-1"

CAM_A = "cam_a"
CAM_B = "cam_b"
SITE_A = "site_a"


# ── In-memory fakes ───────────────────────────────────────────────────────────


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
    """In-memory camera registry with soft-delete semantics (F-1-S6)."""

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

# Camera registry mirrors the grants (F-1-S6): one row per camera.
CAMERAS = {
    CAM_A: Camera(CAM_A, site_id=SITE_A),
    CAM_B: Camera(CAM_B, site_id=None),
}

RECORDS = [
    _record("evt_003", CAM_A, "2026-11-05T10:00:03Z"),
    _record("evt_001", CAM_B, "2026-11-05T10:00:01Z"),
    _record("evt_002", CAM_A, "2026-11-05T10:00:02Z"),
]


# ── Token helpers (same pattern as test_api_auth.py) ─────────────────────────


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
    app = create_app(AppConfig(
        identity_policy=policy,
        grant_repository=grant_repo,
        event_repository=FakeEventRepository(RECORDS),
        camera_repository=cam_repo,
    ))
    return TestClient(app), {"rsa_key": rsa_key}


def _auth(env: dict[str, Any], *, tenant: str = "ten_a",
          roles: list[str] | None = None,
          sites: list[str] | None = None) -> dict[str, str]:
    token = _make_token(env["rsa_key"], tenant=tenant, roles=roles, allowed_sites=sites)
    return {"Authorization": f"Bearer {token}"}


# ── Gate 1: authentication and scoping ────────────────────────────────────────


class TestAuthAndScoping:
    def test_missing_token_401(self, api_client):
        client, _ = api_client
        r = client.get("/api/v1/events")
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


# ── Gate 2: detail reads, cursor contract, problem details ────────────────────


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
        assert r.json()["detail"]["code"] == "not_found"

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
        assert r.json()["detail"]["code"] == "invalid_cursor"

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


# ── Gate 3: no sensitive data leaks ───────────────────────────────────────────


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
        assert r.json()["detail"]["code"] == "grant_data_unusable"


# ── Gate 4: admin-only grant mutation (D5) ────────────────────────────────────


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
        admin = _auth(env, roles=["admin"])
        r = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin,
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
        r = client.delete(f"/api/v1/grants/{CAM_A}", headers=admin)
        assert r.status_code == 200
        assert r.json()["tenant_ids"] == []
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A not in cams


# ── Gate 5: grant request validation (G1) ─────────────────────────────────────


class TestGrantRequestValidation:
    def test_empty_tenant_ids_rejected_422(self, api_client):
        """PUT /grants/{cam} with an empty tenant_ids list must return 422."""
        client, env = api_client
        admin = _auth(env, roles=["admin"])
        r = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin,
            json={"site_id": SITE_A, "tenant_ids": []},
        )
        assert r.status_code == 422


# ── Gate 6: camera soft-delete (F-1-S6) ───────────────────────────────────────


class TestCameraSoftDelete:
    def test_viewer_cannot_delete_camera(self, api_client):
        """Non-admin callers cannot soft-delete a camera."""
        client, env = api_client
        r = client.delete(f"/api/v1/cameras/{CAM_A}", headers=_auth(env))
        assert r.status_code == 401

    def test_admin_soft_delete_sets_deleted_at(self, api_client):
        """DELETE /cameras/{id} sets deleted_at on the camera row."""
        client, env = api_client
        admin = _auth(env, roles=["admin"])
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
        admin = _auth(env, roles=["admin"])
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin)

        # ten_b still has a grant to CAM_A but the camera is deleted.
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A not in cams

    def test_soft_deleted_camera_events_still_accessible(self, api_client):
        """Events of a soft-deleted camera remain accessible to granted tenants.

        The camera row is retained, so event-to-camera mapping resolves and
        the grant still governs access. This is the core rationale for
        soft-delete on the camera entity (ADR-19).
        """
        client, env = api_client
        admin = _auth(env, roles=["admin"])
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin)

        # ten_a still has a grant to CAM_A; events remain visible.
        r = client.get("/api/v1/events", headers=_auth(env, tenant="ten_a"))
        ids = [i["event_id"] for i in r.json()["items"]]
        assert "evt_003" in ids and "evt_002" in ids

    def test_reactivation_via_grant_upsert(self, api_client):
        """PUT /grants/{id} re-activates a soft-deleted camera."""
        client, env = api_client
        admin = _auth(env, roles=["admin"])
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin)

        # Confirm it's gone from inventory.
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A not in cams

        # Re-activate by upserting a grant.
        r = client.put(
            f"/api/v1/grants/{CAM_A}", headers=admin,
            json={"site_id": SITE_A, "tenant_ids": ["ten_a", "ten_b"]},
        )
        assert r.status_code == 200

        # Camera is back in inventory.
        r = client.get("/api/v1/cameras", headers=_auth(env, tenant="ten_b"))
        cams = [c["camera_id"] for c in r.json()["cameras"]]
        assert CAM_A in cams


# ── Gate 7: camera state inspection (F-1-S6) ──────────────────────────────────


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
        admin = _auth(env, roles=["admin"])
        client.delete(f"/api/v1/cameras/{CAM_A}", headers=admin)

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
        assert r.json()["detail"]["code"] == "not_found"
