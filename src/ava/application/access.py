"""Camera access policy for shared static cameras (F-1-S5, F-1-S6).

Defines the trusted camera-grant model, the provider-independent
``GrantRepository`` and ``CameraRepository`` ports and the scope rules that
decide whether a caller may see a camera (and therefore its event history).

Rules (confirmed decisions D1-D8, docs/dev-plan/F-1-S5-01-contract.md):
- A static camera has an optional physical site and explicit grants to one
  or more tenants. Access requires BOTH a grant for the caller's tenant AND
  a site check: a caller restricted to specific sites cannot access a camera
  with no site; a site-unrestricted caller (``site_ids is None``) still
  needs the tenant grant.
- Current grants govern historical event access: new grants expose earlier
  history, revocation removes access. The policy is evaluated per read.
- Missing or malformed trusted grant data denies access; it is never
  inferred.

F-1-S6 adds the camera as a first-class entity (``CameraRepository``) with
soft-delete: deleting a camera hides it from inventory while retaining the
row so that events referencing ``camera_id`` keep a resolvable target
(event-to-camera mapping). Grants remain independently revocable via hard
delete; they are safe to remove because the camera row, not the grant row,
is what events map to.

No FastAPI, SQLAlchemy or database imports here (ADR-16 dependency
direction). Infrastructure adapters implement :class:`GrantRepository` and
:class:`CameraRepository`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .identity import CallerIdentity


@dataclass(frozen=True)
class CameraGrant:
    """Trusted assignment of one static camera.

    ``site_id`` is the canonical ``site_`` ULID or ``None`` when the camera
    has no physical site. ``tenant_ids`` is non-empty for a valid grant set;
    an empty frozenset means "no tenant may access" and is treated as a
    missing assignment (deny). ``updated_at`` is the ISO-8601 UTC timestamp
    of the last trusted change, or ``None`` when unknown (test fakes).
    """

    camera_id: str
    site_id: str | None = None
    tenant_ids: frozenset[str] = frozenset()
    updated_at: str | None = None


@dataclass(frozen=True)
class Camera:
    """Trusted static-camera registry entry (F-1-S6).

    A camera is a first-class entity distinct from its tenant grants.
    ``site_id`` is the canonical ``site_`` ULID or ``None`` when the camera
    has no physical site. ``deleted_at`` is the ISO-8601 UTC timestamp set
    when the camera is soft-deleted, or ``None`` while active. A deleted
    camera is hidden from inventory but its row is retained so that events
    referencing ``camera_id`` keep a resolvable target (event-to-camera
    mapping). Grants are independently revocable via hard delete; they are
    safe to remove because the camera row, not the grant row, is what events
    map to.
    """

    camera_id: str
    site_id: str | None = None
    deleted_at: str | None = None
    updated_at: str | None = None


class GrantLookupError(Exception):
    """Raised when trusted grant data is malformed or unusable.

    Attribute ``camera_id`` identifies the affected camera for diagnostics;
    callers must deny access on this error, never fall back to inference.
    """

    def __init__(self, camera_id: str, reason: str) -> None:
        super().__init__(f"grant data unusable for {camera_id!r}: {reason}")
        self.camera_id = camera_id
        self.reason = reason


@runtime_checkable
class GrantRepository(Protocol):
    """Outbound port: read trusted camera-to-tenant grants.

    Implementations (PostgreSQL ``camera_grants`` table, test fakes) return
    the *current* assignment for a camera. The policy is re-evaluated on
    every read, so grant changes take effect on the next request without
    caching assumptions.

    F-1-S6: when a camera registry is configured, grant replacement must be
    atomic with camera re-activation. Implementations that support the camera
    registry MUST provide :meth:`set_camera_grant_and_reactivate_camera`;
    the application layer rejects configurations that cannot guarantee this
    invariant (ADR-19).
    """

    def get_camera_grant(self, camera_id: str) -> CameraGrant | None:
        """Return the current trusted assignment for *camera_id*.

        Returns ``None`` when the camera has no recorded assignment (deny).
        Raises :class:`GrantLookupError` when data is present but malformed.
        """
        ...

    def list_camera_grants(self) -> list[CameraGrant]:
        """Return all current trusted assignments (camera inventory source)."""
        ...

    def set_camera_grant(
        self, camera_id: str, site_id: str | None, tenant_ids: list[str]
    ) -> None:
        """Create or replace the trusted assignment for *camera_id* (admin)."""
        ...

    def revoke_camera_grant(self, camera_id: str) -> bool:
        """Remove the assignment for *camera_id* (admin). True if a row existed."""
        ...

    def set_camera_grant_and_reactivate_camera(
        self, camera_id: str, site_id: str | None, tenant_ids: list[str]
    ) -> None:
        """Atomically upsert the grant AND re-activate the camera row (F-1-S6).

        Both the grant write and the camera re-activation must commit or roll
        back together. Implementations that span multiple tables MUST use a
        single transaction. This method is required when a camera registry is
        configured; the application layer rejects configurations where the
        grant repository cannot provide it (ADR-19).
        """
        ...


@runtime_checkable
class CameraRepository(Protocol):
    """Outbound port: read and manage the trusted static-camera registry.

    A camera is a first-class entity distinct from its tenant grants
    (F-1-S6). Implementations (PostgreSQL ``cameras`` table, test fakes)
    return the *current* state of each camera. Soft-delete sets
    ``deleted_at`` on the row instead of removing it; deleted cameras are
    hidden from :meth:`list_cameras` and :meth:`get_camera` but the row is
    retained so that events referencing ``camera_id`` keep a resolvable
    target (event-to-camera mapping). Grants are independently revocable via
    hard delete on :class:`GrantRepository`; they are safe to remove because
    the camera row, not the grant row, is what events map to.
    """

    def get_camera(self, camera_id: str) -> Camera | None:
        """Return the current state for *camera_id*, or ``None`` when absent.

        Returns ``None`` for both "no row" and "soft-deleted" — a deleted
        camera is invisible to scoping and inventory queries. Use
        :meth:`find_camera_by_camera_id` to inspect soft-deleted rows.
        """
        ...

    def list_cameras(self) -> list[Camera]:
        """Return all *active* cameras (soft-deleted rows are excluded)."""
        ...

    def upsert_camera(self, camera_id: str, site_id: str | None) -> None:
        """Create or re-activate a camera row (admin).

        Sets ``deleted_at`` to ``NULL`` on conflict, so an existing
        soft-deleted camera is restored to active status.
        """
        ...

    def delete_camera(self, camera_id: str) -> bool:
        """Soft-delete the camera for *camera_id* by setting ``deleted_at``.

        The row is retained with its original state so that event-to-camera
        mapping can reference it. Returns ``True`` when a row was deleted,
        ``False`` when no active row existed (no-op).
        """
        ...

    def find_camera_by_camera_id(self, camera_id: str) -> Camera | None:
        """Return the full camera row for *camera_id*, including soft-deleted rows.

        Used by the admin read endpoint (``GET /cameras/{camera_id}``) to
        inspect camera state including ``deleted_at``. Returns ``None`` when
        no row exists at all.
        """
        ...


class AccessDenied(Exception):
    """Raised when a caller may not access a camera.

    Attributes:
        reason: Machine-readable denial code, one of ``no_grant``,
            ``site_restricted`` or ``grant_data_unusable``.
        camera_id: The camera that was denied.
    """

    def __init__(self, camera_id: str, reason: str) -> None:
        super().__init__(f"access denied for {camera_id!r}: {reason}")
        self.camera_id = camera_id
        self.reason = reason


def camera_accessible(grant: CameraGrant | None, caller: CallerIdentity) -> bool:
    """Return True when *caller* may access the camera described by *grant*.

    Deny-by-default: a ``None`` grant (no trusted assignment) is always
    denied. Site rule: a caller with an explicit ``site_ids`` set (including
    empty) may only access cameras whose ``site_id`` is in that set; a
    camera without a site is therefore invisible to restricted callers. A
    caller with ``site_ids is None`` passes the site check but still needs
    the tenant grant.
    """
    if grant is None:
        return False
    if not grant.tenant_ids or caller.tenant not in grant.tenant_ids:
        return False
    if caller.site_ids is None:
        return True
    return grant.site_id in caller.site_ids


def require_camera_access(
    repository: GrantRepository, camera_id: str, caller: CallerIdentity
) -> CameraGrant:
    """Return the grant for *camera_id* or raise :class:`AccessDenied`.

    Direct-lookup path used by single-object reads. Collection paths must
    apply the equivalent rule via :func:`camera_accessible` so both use one
    set of scope rules. Malformed trusted data denies (never infers).
    """
    try:
        grant = repository.get_camera_grant(camera_id)
    except GrantLookupError as exc:
        raise AccessDenied(camera_id, "grant_data_unusable") from exc
    if grant is None or not camera_accessible(grant, caller):
        if grant is None or not grant.tenant_ids or caller.tenant not in grant.tenant_ids:
            raise AccessDenied(camera_id, "no_grant")
        raise AccessDenied(camera_id, "site_restricted")
    return grant


def visible_cameras(
    repository: GrantRepository, caller: CallerIdentity
) -> list[CameraGrant]:
    """Return the grants for cameras *caller* may access (collection path).

    Uses the same scope rules as :func:`require_camera_access`; cameras with
    missing or malformed assignments are excluded, not guessed.
    """
    visible: list[CameraGrant] = []
    for grant in repository.list_camera_grants():
        if camera_accessible(grant, caller):
            visible.append(grant)
    return visible
