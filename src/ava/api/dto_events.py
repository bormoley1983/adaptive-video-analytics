"""Wire-format DTOs for the S5 read endpoints (events, cameras, grants).

Neutral by design (decision D3): no local paths, RTSP URLs, tenant fields on
events or persistence internals. Ownership is expressed by access — the same
camera and its history render identically for every granted tenant.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EventEvidence(BaseModel):
    """Sanitized evidence metadata; paths never leave the API."""

    kind: str | None = None  # "snapshot" | "clip" | null


class EventItem(BaseModel):
    """One event in a list response (D3 neutral shape)."""

    schema_version: str
    event_id: str
    camera_id: str
    session_id: str
    track_id: int
    rule_id: str
    rule_type: str
    class_label: str
    confidence: float
    source_time_s: float
    emitted_utc: str
    config_version: str
    capture_utc: str | None = None
    time_basis: str | None = None
    evidence: EventEvidence | None = None


class EventsPageResponse(BaseModel):
    """GET /api/v1/events response (cursor pagination, D6)."""

    items: list[EventItem]
    next_cursor: str | None = None


class EventDetailResponse(EventItem):
    """GET /api/v1/events/{event_id} response — same neutral shape."""


class CameraItem(BaseModel):
    """One camera in the inventory response (D3/D4)."""

    camera_id: str
    site_id: str | None = None


class CamerasResponse(BaseModel):
    """GET /api/v1/cameras response."""

    cameras: list[CameraItem]


class GrantRequest(BaseModel):
    """PUT /api/v1/grants/{camera_id} body (admin-only)."""

    site_id: str | None = None
    tenant_ids: list[str] = Field(min_length=1)


class GrantResponse(BaseModel):
    """Current trusted assignment for a camera."""

    camera_id: str
    site_id: str | None = None
    tenant_ids: list[str] = []
    updated_at: str | None = None
