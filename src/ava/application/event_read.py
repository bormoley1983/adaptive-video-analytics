"""Scoped read service for events and cameras (F-1-S5, decisions D3-D8).

Maps trusted grants plus immutable event records to neutral API resources:
no local paths, RTSP URLs or persistence DTO fields leak. Ownership is
expressed by access, not by a tenant field on the event — the same camera
and its history render identically for every tenant that holds a grant.

Cursor contract (D6): global stable order ``(emitted_utc DESC, event_id)``;
opaque base64 cursor encoding ``(emitted_utc, event_id)``; invalid or
foreign cursors raise :class:`InvalidCursorError` (API maps to 400).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from ..events import EventRecord
from .access import CameraGrant, camera_accessible
from .identity import CallerIdentity


class InvalidCursorError(ValueError):
    """Raised when a page cursor is malformed or cannot be decoded."""


@dataclass(frozen=True)
class EventPage:
    """One page of scoped event reads."""

    items: list[EventRecord]
    next_cursor: str | None = None


@dataclass(frozen=True)
class CameraResource:
    """Neutral camera representation for the inventory endpoint (D3)."""

    camera_id: str
    site_id: str | None


def _to_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    # Z-suffix normalization is required for pre-3.11 fromisoformat support.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def encode_cursor(emitted_utc: str, event_id: str) -> str:
    """Encode the (emitted_utc, event_id) boundary into an opaque cursor."""
    raw = json.dumps([emitted_utc, event_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode a cursor back to (emitted_utc, event_id).

    Raises :class:`InvalidCursorError` on any malformed input.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError(f"malformed cursor: {exc}") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(part, str) and part for part in decoded)
    ):
        raise InvalidCursorError("cursor payload must be [emitted_utc, event_id]")
    emitted, event_id = decoded
    try:
        _to_utc(emitted)
    except ValueError as exc:
        raise InvalidCursorError("cursor carries an invalid timestamp") from exc
    return emitted, event_id


def _after_cursor(record: EventRecord, cursor: tuple[str, str]) -> bool:
    """True when *record* sorts strictly after the cursor boundary.

    Order is (emitted_utc DESC, event_id): a record follows the boundary
    when its timestamp is older, or equal with a smaller event_id.
    """
    boundary_ts, boundary_id = cursor
    ts = _to_utc(record.emitted_utc)
    if ts is None:
        # Records without an emitted time cannot be ordered — exclude them
        # from cursor traversal rather than guessing a position.
        return False
    boundary_dt = _to_utc(boundary_ts)
    assert boundary_dt is not None  # validated by decode_cursor
    if ts != boundary_dt:
        return ts < boundary_dt
    return record.event_id < boundary_id


def page_events(
    records: list[EventRecord],
    *,
    grants: dict[str, CameraGrant],
    caller: CallerIdentity,
    cursor: str | None = None,
    limit: int = 50,
) -> EventPage:
    """Return one scoped page of *records* in stable global order.

    Only events whose camera has a grant for the caller's tenant and passes
    the caller's site check are included (D3/D8). ``limit`` must be 1..200.
    """
    if not 1 <= limit <= 200:
        raise ValueError("page size must be between 1 and 200")
    # An empty cursor string is malformed, not "no cursor" — reject it.
    boundary = decode_cursor(cursor) if cursor is not None else None
    visible = [
        r for r in records if camera_accessible(grants.get(r.camera_id), caller)
    ]
    visible.sort(
        key=lambda r: (_to_utc(r.emitted_utc) or datetime.min.replace(tzinfo=UTC), r.event_id),
        reverse=True,
    )
    if boundary is not None:
        visible = [r for r in visible if _after_cursor(r, boundary)]

    page = visible[:limit]
    next_cursor = encode_cursor(page[-1].emitted_utc, page[-1].event_id) if len(page) == limit else None
    return EventPage(items=page, next_cursor=next_cursor)


def find_event(
    record: EventRecord | None,
    *,
    grants: dict[str, CameraGrant],
    caller: CallerIdentity,
) -> tuple[EventRecord, CameraGrant] | None:
    """Return (record, grant) for a single-object read, or None when the
    event is missing, ungranted or site-invisible (API maps to 404)."""
    if record is None:
        return None
    grant = grants.get(record.camera_id)
    if not camera_accessible(grant, caller):
        return None
    assert grant is not None  # implied by camera_accessible returning True
    return record, grant


def list_cameras(
    grants: dict[str, CameraGrant],
    *,
    caller: CallerIdentity,
) -> list[CameraResource]:
    """Inventory of cameras visible to the caller (D4), sorted by ID."""
    resources = [
        CameraResource(camera_id=g.camera_id, site_id=g.site_id)
        for g in grants.values()
        if camera_accessible(g, caller)
    ]
    return sorted(resources, key=lambda c: c.camera_id)
