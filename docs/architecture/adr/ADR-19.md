# ADR-19: Camera soft-delete preserves event-to-camera mapping

Status: accepted (2026-11-06)

## Context

The F-1-S5 read slice stores events with a `camera_id` field that references
the camera's trusted assignment. When an operator removes a camera from the
system, two competing concerns arise:

1. **Inventory hygiene** — deleted cameras should not appear in
   `GET /cameras` or be accessible via scoping queries.
2. **Event-to-camera mapping** — historical events retain their `camera_id`.
   If the camera row is hard-deleted, those events become orphaned: the ID
   still appears in event records but resolves to nothing, breaking audit
   trails and any future feature that joins events back to camera metadata
   (site, install date, etc.).

An earlier attempt added soft-delete directly to the `camera_grants` row.
That conflated two distinct entities: the *camera* (a physical device with a
stable identity) and the *grant* (the tenant-access assignment for that
device). Grants are disposable — revoking access is a routine operation and
no event-to-camera mapping depends on the grant row. Cameras are not — they
carry referential integrity for all historical events.

## Decision

Introduce a first-class `cameras` table (alembic revision 0003) with:

- `camera_id` (PK, Text) — stable camera identity
- `site_id` (nullable Text) — optional physical site
- `deleted_at` (nullable TIMESTAMPTZ) — soft-delete marker
- `updated_at` (TIMESTAMPTZ, default now()) — last change

**Soft-delete on the camera entity:**

- `DELETE /cameras/{camera_id}` (admin-only) sets `deleted_at = now()` on
  the camera row. The row is retained.
- A soft-deleted camera is invisible to `GET /cameras` (inventory) and to
  all scoping queries (`get_camera`, `list_cameras`).
- Events referencing a soft-deleted camera remain stored and accessible to
  their granted tenants — the camera row still exists, so event-to-camera
  mapping resolves.
- Re-activation: an admin may restore a soft-deleted camera by upserting a
  grant (`PUT /grants/{camera_id}`), which clears `deleted_at` via the
  camera upsert path.

**Grants remain hard-deletable:**

- `DELETE /grants/{camera_id}` (admin-only) performs a hard DELETE on the
  `camera_grants` row. No retention is needed because events map to the
  camera row, not the grant row.
- Revoking a grant removes tenant access immediately; it does not affect
  event-to-camera mapping.

**Admin read endpoint:**

- `GET /cameras/{camera_id}` (admin-only) returns the full camera row
  including `deleted_at`, so operators can inspect soft-deleted cameras.
  Returns 404 when no row exists at all.

## Consequences

- New table + migration: one additional Alembic revision (0003) creating
  `cameras`. The earlier attempt to add `deleted_at` to `camera_grants` was
  reverted; the 0003 slot is reused for the cameras table.
- New port: `CameraRepository` protocol in `application/access.py` with
  `get_camera`, `list_cameras`, `upsert_camera`, `delete_camera`, and
  `find_camera_by_camera_id`.
- New adapter: `PostgresCameraRepository` in
  `persistence/cameras_postgres.py`.
- New DTO: `CameraState` in `api/dto_events.py` for the admin read endpoint.
- New routes: `DELETE /cameras/{camera_id}` and `GET /cameras/{camera_id}`
  in `api/routes_events.py`, both admin-only.
- OpenAPI contract updated with the two new operations and the `CameraState`
  schema.
- `AppConfig` gains a `camera_repository` field; when set, the camera admin
  routes are enabled.
- The `GrantRepository` port is unchanged from F-1-S5 (no soft-delete
  methods added).

## Alternatives considered

**Soft-delete on grants (reverted):** Added `deleted_at` to `camera_grants`.
Rejected because it conflated the camera entity with its access assignment.
Grants are safe to hard-delete; the retention requirement belongs to the
camera, not the grant.

**No soft-delete (hard delete cameras):** Simplest option but orphans all
historical events for a deleted camera. Event-to-camera mapping breaks,
audit trails lose referential integrity, and any future join of events to
camera metadata fails. Rejected.

**Separate tombstone table:** A `deleted_cameras` table tracking removals
while keeping the main `cameras` table clean. Adds query complexity (every
inventory read must check both tables) without benefit — a nullable
`deleted_at` on the same row is simpler and keeps all camera state in one
place. Rejected.
