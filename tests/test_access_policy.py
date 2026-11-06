"""Unit tests for the camera access policy (F-1-S5 deliverable 02).

Deterministic, provider-independent: a fake GrantRepository exercises the
scope rules without any database. Covers two tenants sharing one camera,
tenants without grants, all-site versus site-restricted callers, site-less
deny for restricted callers, revoked/new grants and missing/malformed
trusted grant data.
"""

from __future__ import annotations

import pytest

from ava.application.access import (
    AccessDenied,
    CameraGrant,
    GrantLookupError,
    camera_accessible,
    require_camera_access,
    visible_cameras,
)
from ava.application.identity import CallerIdentity


class FakeGrantRepository:
    """In-memory GrantRepository for policy tests."""

    def __init__(self, grants: dict[str, CameraGrant | None]) -> None:
        self._grants = dict(grants)
        self._malformed: set[str] = set()

    def get_camera_grant(self, camera_id: str) -> CameraGrant | None:
        if camera_id in self._malformed:
            raise GrantLookupError(camera_id, "simulated malformed row")
        return self._grants.get(camera_id)

    def list_camera_grants(self) -> list[CameraGrant]:
        out = []
        for cam, grant in self._grants.items():
            if cam in self._malformed:
                continue  # malformed rows are not enumerable assignments
            if grant is not None:
                out.append(grant)
        return out

    def mark_malformed(self, camera_id: str) -> None:
        self._malformed.add(camera_id)


def _caller(
    tenant: str = "ten_a", site_ids: frozenset[str] | None = None
) -> CallerIdentity:
    return CallerIdentity(
        subject=f"user@{tenant}",
        tenant=tenant,
        roles=frozenset({"viewer"}),
        site_ids=site_ids,
    )


CAMERA = "cam_01"
OTHER_CAMERA = "cam_02"
SITE_A = "site_a"
SITE_B = "site_b"


class TestTwoTenantsShareOneCamera:
    def test_both_granted_tenants_can_access(self) -> None:
        grant = CameraGrant(
            CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a", "ten_b"})
        )
        repo = FakeGrantRepository({CAMERA: grant})
        assert camera_accessible(grant, _caller("ten_a"))
        assert camera_accessible(grant, _caller("ten_b"))
        require_camera_access(repo, CAMERA, _caller("ten_a"))
        require_camera_access(repo, CAMERA, _caller("ten_b"))

    def test_ungranted_tenant_denied(self) -> None:
        grant = CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a"}))
        repo = FakeGrantRepository({CAMERA: grant})
        assert not camera_accessible(grant, _caller("ten_b"))
        with pytest.raises(AccessDenied) as exc:
            require_camera_access(repo, CAMERA, _caller("ten_b"))
        assert exc.value.reason == "no_grant"

    def test_empty_tenant_ids_denies_everyone(self) -> None:
        grant = CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset())
        repo = FakeGrantRepository({CAMERA: grant})
        assert not camera_accessible(grant, _caller("ten_a"))
        with pytest.raises(AccessDenied) as exc:
            require_camera_access(repo, CAMERA, _caller("ten_a"))
        assert exc.value.reason == "no_grant"


class TestSiteScope:
    def test_all_site_caller_sees_sited_and_siteless_cameras(self) -> None:
        sited = CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a"}))
        siteless = CameraGrant(OTHER_CAMERA, site_id=None, tenant_ids=frozenset({"ten_a"}))
        caller = _caller("ten_a", site_ids=None)
        assert camera_accessible(sited, caller)
        assert camera_accessible(siteless, caller)

    def test_site_restricted_caller_sees_matching_site_only(self) -> None:
        sited = CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a"}))
        other = CameraGrant(OTHER_CAMERA, site_id=SITE_B, tenant_ids=frozenset({"ten_a"}))
        caller = _caller("ten_a", site_ids=frozenset({SITE_A}))
        assert camera_accessible(sited, caller)
        assert not camera_accessible(other, caller)

    def test_site_restricted_caller_denied_for_siteless_camera(self) -> None:
        siteless = CameraGrant(CAMERA, site_id=None, tenant_ids=frozenset({"ten_a"}))
        repo = FakeGrantRepository({CAMERA: siteless})
        caller = _caller("ten_a", site_ids=frozenset({SITE_A}))
        assert not camera_accessible(siteless, caller)
        with pytest.raises(AccessDenied) as exc:
            require_camera_access(repo, CAMERA, caller)
        assert exc.value.reason == "site_restricted"

    def test_empty_site_set_denies_all_cameras(self) -> None:
        sited = CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a"}))
        caller = _caller("ten_a", site_ids=frozenset())
        assert not camera_accessible(sited, caller)

    def test_site_unrestricted_never_bypasses_tenant_grant(self) -> None:
        grant = CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_b"}))
        caller = _caller("ten_a", site_ids=None)
        assert not camera_accessible(grant, caller)


class TestGrantChanges:
    def test_new_grant_exposes_camera_on_next_read(self) -> None:
        repo = FakeGrantRepository({})
        with pytest.raises(AccessDenied):
            require_camera_access(repo, CAMERA, _caller("ten_b"))
        # Grant added at runtime (current grants govern access per read).
        repo._grants[CAMERA] = CameraGrant(
            CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_b"})
        )
        require_camera_access(repo, CAMERA, _caller("ten_b"))

    def test_revocation_removes_access_on_next_read(self) -> None:
        repo = FakeGrantRepository(
            {CAMERA: CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a", "ten_b"}))}
        )
        require_camera_access(repo, CAMERA, _caller("ten_b"))
        # Revocation: ten_b removed from the current grant set.
        repo._grants[CAMERA] = CameraGrant(
            CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a"})
        )
        with pytest.raises(AccessDenied) as exc:
            require_camera_access(repo, CAMERA, _caller("ten_b"))
        assert exc.value.reason == "no_grant"


class TestMissingOrMalformedGrants:
    def test_missing_assignment_denies(self) -> None:
        assert not camera_accessible(None, _caller("ten_a"))
        repo = FakeGrantRepository({})
        with pytest.raises(AccessDenied) as exc:
            require_camera_access(repo, CAMERA, _caller("ten_a"))
        assert exc.value.reason == "no_grant"

    def test_malformed_grant_data_denies_lookup(self) -> None:
        repo = FakeGrantRepository({CAMERA: CameraGrant(CAMERA)})
        repo.mark_malformed(CAMERA)
        with pytest.raises(AccessDenied) as exc:
            require_camera_access(repo, CAMERA, _caller("ten_a"))
        assert exc.value.reason == "grant_data_unusable"

    def test_malformed_grant_excluded_from_collection(self) -> None:
        good = CameraGrant(OTHER_CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a"}))
        repo = FakeGrantRepository({CAMERA: CameraGrant(CAMERA), OTHER_CAMERA: good})
        repo.mark_malformed(CAMERA)
        visible = visible_cameras(repo, _caller("ten_a"))
        assert [g.camera_id for g in visible] == [OTHER_CAMERA]

    def test_collection_and_lookup_use_equivalent_rules(self) -> None:
        grants: dict[str, CameraGrant | None] = {
            CAMERA: CameraGrant(CAMERA, site_id=SITE_A, tenant_ids=frozenset({"ten_a"})),
            OTHER_CAMERA: CameraGrant(OTHER_CAMERA, site_id=None, tenant_ids=frozenset({"ten_a"})),
        }
        repo = FakeGrantRepository(grants)
        restricted = _caller("ten_a", site_ids=frozenset({SITE_A}))
        visible_ids = {g.camera_id for g in visible_cameras(repo, restricted)}
        assert visible_ids == {CAMERA}
        # Lookup agrees with the collection view.
        require_camera_access(repo, CAMERA, restricted)
        with pytest.raises(AccessDenied):
            require_camera_access(repo, OTHER_CAMERA, restricted)
