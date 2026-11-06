"""Unit tests for the scoped event read service (F-1-S5 deliverable 03).

Deterministic: in-memory grants and synthetic EventRecords exercise scoping,
cursor ordering/bounds and the neutral mapping rules without any database.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from ava.application.access import CameraGrant
from ava.application.event_read import (
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
    find_event,
    list_cameras,
    page_events,
)
from ava.application.identity import CallerIdentity
from ava.events import EventRecord, EvidenceLocator


def _record(
    event_id: str,
    camera_id: str,
    emitted_utc: str,
    source_time_s: float = 1.0,
) -> EventRecord:
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
        source_time_s=source_time_s,
        emitted_utc=emitted_utc,
        config_version="1",
        model_ref=None,
        evidence=EvidenceLocator(),
    )


def _caller(tenant: str = "ten_a", site_ids: frozenset[str] | None = None) -> CallerIdentity:
    return CallerIdentity(
        subject=f"user@{tenant}", tenant=tenant, roles=frozenset({"viewer"}), site_ids=site_ids
    )


CAM_A = "cam_a"
CAM_B = "cam_b"
SITE_A = "site_a"

# Shared camera: granted to both tenants; B is site-less.
SHARED_GRANTS = {
    CAM_A: CameraGrant(CAM_A, site_id=SITE_A, tenant_ids=frozenset({"ten_a", "ten_b"})),
    CAM_B: CameraGrant(CAM_B, site_id=None, tenant_ids=frozenset({"ten_a", "ten_b"})),
}

T1 = "2026-11-05T10:00:03Z"
T2 = "2026-11-05T10:00:02Z"
T3 = "2026-11-05T10:00:01Z"


class TestScoping:
    def test_ungranted_camera_events_invisible(self) -> None:
        records = [
            _record("evt_001", CAM_A, T1),
            _record("evt_002", "cam_unknown", T2),
        ]
        page = page_events(records, grants=SHARED_GRANTS, caller=_caller("ten_a"))
        assert [r.event_id for r in page.items] == ["evt_001"]

    def test_tenant_without_grant_sees_nothing(self) -> None:
        records = [_record("evt_001", CAM_A, T1)]
        page = page_events(records, grants=SHARED_GRANTS, caller=_caller("ten_c"))
        assert page.items == []

    def test_site_restricted_caller_excludes_siteless_camera(self) -> None:
        records = [
            _record("evt_001", CAM_A, T1),
            _record("evt_002", CAM_B, T2),
        ]
        page = page_events(
            records, grants=SHARED_GRANTS, caller=_caller("ten_a", frozenset({SITE_A}))
        )
        assert [r.event_id for r in page.items] == ["evt_001"]

    def test_both_tenants_see_shared_camera_history(self) -> None:
        records = [_record("evt_001", CAM_A, T1), _record("evt_002", CAM_A, T2)]
        for tenant in ("ten_a", "ten_b"):
            page = page_events(records, grants=SHARED_GRANTS, caller=_caller(tenant))
            assert {r.event_id for r in page.items} == {"evt_001", "evt_002"}

    def test_find_event_denies_cross_scope(self) -> None:
        record = _record("evt_002", CAM_B, T2)
        assert find_event(
            record, grants=SHARED_GRANTS, caller=_caller("ten_a", frozenset({SITE_A}))
        ) is None
        found = find_event(record, grants=SHARED_GRANTS, caller=_caller("ten_b"))
        assert found is not None and found[0].event_id == "evt_002"

    def test_find_event_missing_record(self) -> None:
        assert find_event(None, grants=SHARED_GRANTS, caller=_caller("ten_a")) is None


class TestOrderingAndPagination:
    RECORDS: ClassVar[list[EventRecord]] = [
        _record("evt_c", CAM_A, T1),
        _record("evt_a", CAM_B, T3),
        _record("evt_b", CAM_A, T2),
    ]

    def test_global_order_emitted_desc_then_event_id(self) -> None:
        page = page_events(self.RECORDS, grants=SHARED_GRANTS, caller=_caller("ten_a"), limit=10)
        assert [r.event_id for r in page.items] == ["evt_c", "evt_b", "evt_a"]
        assert page.next_cursor is None  # fewer than limit items

    def test_equal_timestamps_order_by_event_id_desc(self) -> None:
        records = [
            _record("evt_002", CAM_A, T1),
            _record("evt_001", CAM_B, T1),
        ]
        page = page_events(records, grants=SHARED_GRANTS, caller=_caller("ten_a"), limit=10)
        assert [r.event_id for r in page.items] == ["evt_002", "evt_001"]

    def test_cursor_traversal_covers_all_without_overlap(self) -> None:
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            page = page_events(
                self.RECORDS, grants=SHARED_GRANTS, caller=_caller("ten_a"),
                cursor=cursor, limit=2,
            )
            seen.extend(r.event_id for r in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert seen == ["evt_c", "evt_b", "evt_a"]

    def test_page_size_bounds(self) -> None:
        with pytest.raises(ValueError):
            page_events(self.RECORDS, grants=SHARED_GRANTS, caller=_caller("ten_a"), limit=0)
        with pytest.raises(ValueError):
            page_events(self.RECORDS, grants=SHARED_GRANTS, caller=_caller("ten_a"), limit=201)

    def test_invalid_cursor_rejected(self) -> None:
        for bad in ("!!!not-base64!!!", "aGVsbG8=", "", "YWJjZA==", "bm90anNvbg=="):
            with pytest.raises(InvalidCursorError):
                page_events(
                    self.RECORDS, grants=SHARED_GRANTS, caller=_caller("ten_a"), cursor=bad
                )

    def test_cursor_with_bad_payload_rejected(self) -> None:
        import base64
        import json

        bad = base64.urlsafe_b64encode(json.dumps(["not-a-time", "evt_x"]).encode()).decode()
        with pytest.raises(InvalidCursorError):
            page_events(self.RECORDS, grants=SHARED_GRANTS, caller=_caller("ten_a"), cursor=bad)

    def test_cursor_roundtrip(self) -> None:
        cursor = encode_cursor(T1, "evt_c")
        assert decode_cursor(cursor) == (T1, "evt_c")


class TestCamerasInventory:
    def test_inventory_visible_cameras_sorted(self) -> None:
        resources = list_cameras(SHARED_GRANTS, caller=_caller("ten_a"))
        assert [(r.camera_id, r.site_id) for r in resources] == [
            (CAM_A, SITE_A),
            (CAM_B, None),
        ]

    def test_inventory_site_restricted(self) -> None:
        resources = list_cameras(SHARED_GRANTS, caller=_caller("ten_a", frozenset({SITE_A})))
        assert [r.camera_id for r in resources] == [CAM_A]

    def test_inventory_ungranted_tenant_empty(self) -> None:
        assert list_cameras(SHARED_GRANTS, caller=_caller("ten_c")) == []
