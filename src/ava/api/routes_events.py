"""Authenticated event reads and transactional camera/grant mutations.

Mutation routes invoke one application workflow; persistence owns the
transaction, scope locks and stored retry results.
"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..application.access import AccessDenied, GrantLookupError, require_camera_access
from ..application.event_read import (
    InvalidCursorError,
    find_event,
    list_cameras,
    page_events,
)
from ..application.identity import CallerIdentity
from ..application.mutation import (
    REPRESENTATION_VERSION,
    IdempotencyKeyReused,
    InvalidIfMatch,
    MutationAdmission,
    MutationScope,
    PreconditionFailed,
    RequestInProgress,
    TargetNotFound,
    compute_fingerprint,
    execute_idempotent_mutation,
    make_etag,
)
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

logger = logging.getLogger(__name__)

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
    title = {
        400: "Bad Request",
        404: "Not Found",
        409: "Conflict",
        412: "Precondition Failed",
        502: "Bad Gateway",
    }.get(status, "Error")
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


def _key_hash(namespace: str, key: str) -> str:
    """SHA-256 hex digest of the idempotency key."""
    return hashlib.sha256(json.dumps([namespace, key], separators=(",", ":")).encode("utf-8")).hexdigest()


def _namespace(caller: CallerIdentity) -> str:
    """Caller scope for idempotency key namespacing (tenant:subject)."""
    return json.dumps([caller.tenant, caller.subject], separators=(",", ":"))


@router.get("/events", response_model=EventsPageResponse)
def list_events(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    caller: CallerIdentity = Depends(_caller),  # noqa: B008
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
    .
    """
    state = request.app.state
    try:
        grants = {g.camera_id: g for g in state.grant_repository.list_camera_grants()}
    except GrantLookupError as exc:
        raise _problem(502, "grant_data_unusable", str(exc)) from exc
    active_ids: set[str] | None = None
    camera_repo = state.camera_repository
    if camera_repo is not None:
        active_ids = {c.camera_id for c in camera_repo.list_cameras()}
    resources = list_cameras(grants, caller=caller, active_camera_ids=active_ids)
    return CamerasResponse(
        cameras=[CameraItem(camera_id=c.camera_id, site_id=c.site_id) for c in resources]
    )


@router.get("/grants/{camera_id}", response_model=GrantResponse)
def get_grant(
    camera_id: str,
    request: Request,
    response: Response,
    caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> GrantResponse:
    """Inspect the trusted assignment for a camera (admin-only).

    Returns the grant and its strong ETag, or 404 when no grant exists.
    the ETag is resource-specific (resource_type="grant") so it
    differs from the camera's own ETag even at the same revision.
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")
    state = request.app.state
    grant, revision = state.grant_repository.get_camera_grant_with_revision(camera_id)
    if grant is None:
        raise _problem(404, "not_found", "no grant for this camera")
    response.headers["ETag"] = make_etag(
        "grant", camera_id, REPRESENTATION_VERSION, revision
    )
    return GrantResponse(
        camera_id=grant.camera_id,
        site_id=grant.site_id,
        tenant_ids=sorted(grant.tenant_ids),
        updated_at=grant.updated_at,
    )


@router.get("/cameras/{camera_id}", response_model=CameraState)
def get_camera_state(
    camera_id: str,
    request: Request,
    response: Response,
    caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> CameraState:
    """Inspect the full state of a camera including soft-delete (admin-only).

    Returns the complete camera row including ``deleted_at`` so operators can
    inspect soft-deleted cameras. Returns 404 when no row exists at all.

    the response carries an ``ETag`` header with the camera's
    resource-specific strong ETag (resource_type="camera").
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

    response.headers["ETag"] = make_etag(
        "camera", camera_id, REPRESENTATION_VERSION, camera.revision
    )
    return CameraState(
        camera_id=camera.camera_id,
        site_id=camera.site_id,
        deleted_at=camera.deleted_at,
        updated_at=camera.updated_at,
    )


async def _execute_mutation(
    request: Request,
    caller: CallerIdentity,
    camera_id: str,
    operation: str,
    params: dict,
    if_match_raw: str | None,
    path: str,
    body: dict | None = None,
) -> tuple[int, dict | None, str | None]:
    """Shared mutation executor: admission → idempotent workflow → result.

    Returns (status_code, body, etag). Raises HTTPException for 409/412/400.

    Requests fail closed when no transaction factory is configured.
    """
    state = request.app.state
    txn = state.mutation_txn

    if txn is None:
        raise _problem(502, "mutation_unavailable", "A transactional mutation adapter is required")
    txn = txn.new_transaction(operation)

    admission: MutationAdmission = state.mutation_admission
    if not admission.try_acquire():
        raise _problem(
            409, "request_in_progress",
            "mutation admission limit reached; retry later",
        )
    try:
        key = request.state.idempotency_key
        namespace = _namespace(caller)
        key_hash = _key_hash(namespace, key)
        fingerprint = compute_fingerprint(
            request.method, path, body=body, if_match=if_match_raw
        )
        scope = MutationScope(key=f"camera:{camera_id}")

        result = await execute_idempotent_mutation(
            txn=txn,
            namespace=namespace,
            key_hash=key_hash,
            fingerprint=fingerprint,
            scope=scope,
            camera_id=camera_id,
            if_match_raw=if_match_raw,
            operation=operation,
            params=params,
        )
        return (result.status_code, result.body, result.etag)

    except TargetNotFound:
        raise _problem(404, "not_found", "camera not found") from None
    except IdempotencyKeyReused:
        raise _problem(
            409, "idempotency_key_reused",
            "Idempotency key was already used for a different operation",
        ) from None
    except RequestInProgress:
        raise _problem(
            409, "request_in_progress",
            "A concurrent request with the same idempotency key is in progress",
        ) from None
    except PreconditionFailed as exc:
        raise _problem(
            412, "precondition_failed", str(exc),
        ) from exc
    except InvalidIfMatch as exc:
        raise _problem(400, "invalid_if_match", str(exc)) from exc
    finally:
        admission.release()


@router.put("/grants/{camera_id}", response_model=GrantResponse)
async def set_grant(
    camera_id: str,
    body: GrantRequest,
    request: Request,
    response: Response,
    caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> GrantResponse:
    """Replace the trusted assignment for a camera (admin-only, D5).

    idempotent mutation with atomic claim-before-mutate. The grant
    upsert and revision advance occur within a single transaction alongside
    the idempotency record. If-Match is evaluated atomically against the
    target's current ETag within the same transaction.

    Returns 200 with GrantResponse and the updated grant ETag.
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")

    if_match_raw = request.headers.get("If-Match")
    path = f"/api/v1/grants/{camera_id}"
    body_dict = body.model_dump()

    _status, result_body, etag = await _execute_mutation(
        request, caller, camera_id, "grant_put",
        params={"site_id": body.site_id, "tenant_ids": list(body.tenant_ids)},
        if_match_raw=if_match_raw, path=path, body=body_dict,
    )

    if etag is not None:
        response.headers["ETag"] = etag

    # Map the result body to GrantResponse.
    if result_body is None:
        raise _problem(502, "grant_data_unusable", "grant write did not persist")
    return GrantResponse(**result_body)


@router.delete("/grants/{camera_id}", status_code=204)
async def revoke_grant(
    camera_id: str, request: Request, caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> Response:
    """Remove the trusted assignment for a camera (admin-only, D5).

    idempotent mutation. Returns 204 whether or not a grant existed
    (absent grant → no-op, still 204). The revision advances only when a
    grant actually existed. If-Match is evaluated atomically within the
    transaction.
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")

    if_match_raw = request.headers.get("If-Match")
    path = f"/api/v1/grants/{camera_id}"

    status, _, _ = await _execute_mutation(
        request, caller, camera_id, "grant_delete",
        params={}, if_match_raw=if_match_raw, path=path,
    )
    return Response(status_code=status)


@router.delete("/cameras/{camera_id}", status_code=204)
async def delete_camera(
    camera_id: str, request: Request, caller: CallerIdentity = Depends(_caller),  # noqa: B008
) -> Response:
    """Soft-delete a camera.

    idempotent mutation. Returns 204 when an active camera was
    deleted or was already soft-deleted (no-op). Returns 404 when the
    camera never existed. The revision advances on first deletion only.
    If-Match is evaluated atomically within the transaction.
    """
    if "admin" not in caller.roles:
        reject_authentication("insufficient_role", "admin role required")

    if_match_raw = request.headers.get("If-Match")
    path = f"/api/v1/cameras/{camera_id}"

    status, _, _ = await _execute_mutation(
        request, caller, camera_id, "camera_delete",
        params={}, if_match_raw=if_match_raw, path=path,
    )
    return Response(status_code=status)
