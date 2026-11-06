"""Camera access policy for shared static cameras (F-1-S5).

Defines the trusted camera-grant model, the provider-independent
``GrantRepository`` port and the scope rules that decide whether a caller
may see a camera (and therefore its event history).

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

No FastAPI, SQLAlchemy or database imports here (ADR-16 dependency
direction). Infrastructure adapters implement :class:`GrantRepository`.
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
