"""Authenticated read routes for events, cameras and grants (F-1-S5, F-1-S6).

Each route invokes one focused application use case and maps its result to
the public contract. No SQL, S3, broker or business state transitions here
(ADR-16); grant mutation is the only write in the S5 slice and is admin-only
(decision D5). F-1-S6 adds camera soft-delete (``DELETE /cameras/{id}``) and
state inspection (``GET /cameras/{id}``), both admin-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..application.access import AccessDenied, GrantLookupError, require_camera_access
from ..application.event_read import (
    InvalidCursorError,
    find_event,
    list_cameras,
    page_events,
)
from ..application.identity import CallerIdentity
from .auth import reject_authentication
from .dto_events import (
    CameraItem,
    CamerasResponse,
    CameraState,
    EventDetailResponse,
    EventEvidence,
    EventItem,
    EventsPageResponse,
    GrantRequest,
    GrantResponse,
)

router = APIRouter()


def _caller(request: Request) -> CallerIdentity:
    """Authenticated caller for this application's identity policy."""
    from .auth import create_auth_dependency

    policy = request.app.state.identity_policy
    if policy is None:
        reject_authentication(
            "authentication_unavailable", "Authentication is not configured"
        )
    return create_auth_dependency(policy)(request)


def _problem(status: int, code: str, detail: str) -> HTTPException:
    """Build an RFC 9457 problem detail (handled by the app's handler)."""
    title = {400: "Bad Request", 404: "Not Found", 502: "Bad Gateway"}.get(
        status, "Error"
    )
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status,
                "code": code, "detail": detail},
    )


def _to_item(record) -> EventItem:
    """Map a canonical event record to the neutral wire shape (D3)."""
    evidence = None
    if record.evidence is not None and record.evidence.kind:
        evidence = EventEvidence(kind=record.evidence.kind)
    return EventItem(
        schema_version=record.schema_version,
        event_id=record.event_id,
        camera_id=record.camera_id,
        session_id=record.session_id,
        track_id=record.track_id,
        rule_id=record.rule_id,
        rule_type=record.rule_type,
        class_label=record.class_label,
        confidence=record.confidence,
        source_time_s=record.source_time_s,
        emitted_utc=record.emitted_utc,
        config_version=record.config_version,
        capture_utc=record.capture_utc,
        time_basis=record.time_basis if record.time_basis != "file_pts_only" else None,
        evidence=evidence,
    )


@router.get("/events", response_model=EventsPageResponse)
def list_events(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    caller: CallerIdentity = Depends(_caller),  # noqa: B008,
) -> EventsPageResponse:
    """Scoped event collection with cursor pagination (D4/D6)."""
    state = request.app.state
    try:
        grants = {g.camera_id: g for g in state.grant_repository.list_camera_grants()}
    except GrantLookupError as exc:
        raise _problem(502, "grant_data_unusable", str(exc)) from exc
    records = state.event_repository.list_all()
    try:
        page = page_events(
            records, grants=grants, caller=caller, cursor=cursor, limit=limit
        )
    except InvalidCursorError as exc:
        raise _problem(400, "invalid_cursor", str(exc)) from exc
    return EventsPageResponse(
        items=[_to_item(r) for r in page.items], next_cursor=page.next_cursor
    )


@router.get("/events/{event_id}", response_model=EventDetailResponse)
def get_event(
    event_id: str, request: Request, caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> EventDetailResponse:
    """Single-object event read; 404 when missing or inaccessible (D8)."""
    state = request.app.state
    record = state.event_repository.find_by_event_id(event_id)
    if record is None:
        raise _problem(404, "not_found", "event not found")
    try:
        grant = require_camera_access(state.grant_repository, record.camera_id, caller)
    except (AccessDenied, GrantLookupError):
        raise _problem(404, "not_found", "event not found") from None
    result = find_event(record, grants={record.camera_id: grant}, caller=caller)
    if result is None:
        raise _problem(404, "not_found", "event not found")
    return EventDetailResponse(**_to_item(result[0]).model_dump())


@router.get("/cameras", response_model=CamerasResponse)
def list_cameras_route(
    request: Request, caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> CamerasResponse:
    """Inventory of cameras visible to the caller (D4).

    Soft-deleted cameras are excluded when a camera registry is configured
    (F-1-S6, ADR-19).
    """
    state = request.app.state
    try:
        grants = {g.camera_id: g for g in state.grant_repository.list_camera_grants()}
    except GrantLookupError as exc:
        raise _problem(502, "grant_data_unusable", str(exc)) from exc
    # F-1-S6: filter out soft-deleted cameras when the registry is available.
    active_ids: set[str] | None = None
    camera_repo = state.camera_repository
    if camera_repo is not None:
        active_ids = {c.camera_id for c in camera_repo.list_cameras()}
    resources = list_cameras(grants, caller=caller, active_camera_ids=active_ids)
    return CamerasResponse(
        cameras=[CameraItem(camera_id=c.camera_id, site_id=c.site_id) for c in resources]
    )


@router.put("/grants/{camera_id}", response_model=GrantResponse)
def set_grant(
    camera_id: str,
    body: GrantRequest,
    request: Request,
    caller: CallerIdentity = Depends(_caller),  # noqa: B008,
) -> GrantResponse:
    """Replace the trusted assignment for a camera (admin-only, D5).

    When a camera registry is configured, the grant upsert and camera
    re-activation are performed atomically in a single transaction
    (F-1-S6, ADR-19) so a partial failure cannot leave the camera active
    while the old grant still applies.
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")
    state = request.app.state
    # F-1-S6: atomic grant + camera re-activation when registry is available.
    # Configurations that cannot provide the atomic path are rejected at
    # composition time (create_app); this check is a defensive guard.
    camera_repo = state.camera_repository
    if camera_repo is not None:
        grant_repo = state.grant_repository
        if not hasattr(grant_repo, "set_camera_grant_and_reactivate_camera"):
            raise _problem(
                502,
                "atomic_grant_unavailable",
                "grant repository does not support atomic camera re-activation; "
                "use compose_postgres_app or a GrantRepository that implements "
                "set_camera_grant_and_reactivate_camera (ADR-19)",
            )
        grant_repo.set_camera_grant_and_reactivate_camera(
            camera_id, body.site_id, list(body.tenant_ids)
        )
    else:
        state.grant_repository.set_camera_grant(
            camera_id, body.site_id, list(body.tenant_ids)
        )
    grant = state.grant_repository.get_camera_grant(camera_id)
    if grant is None:
        raise _problem(502, "grant_data_unusable", "grant write did not persist")
    return GrantResponse(
        camera_id=grant.camera_id,
        site_id=grant.site_id,
        tenant_ids=sorted(grant.tenant_ids),
        updated_at=grant.updated_at,
    )


@router.delete("/grants/{camera_id}", response_model=GrantResponse)
def revoke_grant(
    camera_id: str, request: Request, caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> GrantResponse:
    """Remove the trusted assignment for a camera (admin-only, D5).

    Grants are safe to hard-delete: events map to the camera row, not the
    grant row, so removing a grant never breaks event-to-camera mapping.
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")
    state = request.app.state
    state.grant_repository.revoke_camera_grant(camera_id)
    grant = state.grant_repository.get_camera_grant(camera_id)
    return GrantResponse(
        camera_id=camera_id,
        site_id=grant.site_id if grant else None,
        tenant_ids=[],
        updated_at=grant.updated_at if grant else None,
    )


@router.delete("/cameras/{camera_id}", status_code=204)
def delete_camera(
    camera_id: str, request: Request, caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> Response:
    """Soft-delete a camera (admin-only, F-1-S6).

    Sets ``deleted_at`` on the camera row instead of removing it. The row is
    retained so that events referencing ``camera_id`` keep a resolvable target
    (event-to-camera mapping). The camera becomes invisible to inventory and
    scoping queries until re-activated via grant upsert. Returns 204 when an
    active camera was deleted, 404 when no active camera exists.
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")
    state = request.app.state
    camera_repo = state.camera_repository
    if camera_repo is None:
        raise _problem(502, "camera_registry_unavailable", "camera registry is not configured")
    deleted = camera_repo.delete_camera(camera_id)
    if not deleted:
        raise _problem(404, "not_found", "no active camera with this id")
    return Response(status_code=204)


@router.get("/cameras/{camera_id}", response_model=CameraState)
def get_camera_state(
    camera_id: str, request: Request, caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> CameraState:
    """Inspect the full state of a camera including soft-delete (admin-only, F-1-S6).

    Returns the complete camera row including ``deleted_at`` so operators can
    inspect soft-deleted cameras. Returns 404 when no row exists at all
    (never created or hard-removed — but cameras are only ever soft-deleted,
    so this means "not found").
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")
    state = request.app.state
    camera_repo = state.camera_repository
    if camera_repo is None:
        raise _problem(502, "camera_registry_unavailable", "camera registry is not configured")
    camera = camera_repo.find_camera_by_camera_id(camera_id)
    if camera is None:
        raise _problem(404, "not_found", "camera not found")
    return CameraState(
        camera_id=camera.camera_id,
        site_id=camera.site_id,
        deleted_at=camera.deleted_at,
        updated_at=camera.updated_at,
    )
