"""E-2-T2 rule engine fixtures (deterministic, no GPU).

Covers the acceptance criteria: boundary jitter, sparse samples, re-entry,
missing tracks and timestamp discontinuities, with explicit expected counts
and times (ADR-4 semantics).
"""

from __future__ import annotations

import math

import pytest

from ava.config import (
    ClassFilter,
    DwellRule,
    LineCrossingRule,
    LineGeometry,
    PolygonGeometry,
    ZoneEntryRule,
)
from ava.detector import Detection, FrameResult
from ava.rules import (
    RuleEngine,
    bottom_center_anchor,
    line_side,
    point_in_polygon,
    segments_cross_line,
)

FPS = 10.0


def det(track_id: int, x: float, y_bottom: float, label: str = "person",
        conf: float = 0.9, box_h: float = 0.2) -> Detection:
    """Build a detection whose bottom-center anchor is (x, y_bottom)."""
    return Detection(
        class_id=0,
        label=label,
        confidence=conf,
        box=(x - 0.05, y_bottom - box_h, x + 0.05, y_bottom),
        track_id=track_id,
    )


def frame(index: int, timestamp_s: float, dets: list[Detection]) -> FrameResult:
    return FrameResult(index, timestamp_s, 1920, 1080, dets)


def run_engine(rules: list, frames: list[FrameResult], camera="cam-01",
               session="s1") -> list:
    engine = RuleEngine(rules, camera_id=camera, session_id=session)
    events = []
    for f in frames:
        events.extend(engine.process_frame(f, emitted_utc=f"synthetic-{f.index}"))
    return events


# --- geometry unit checks ----------------------------------------------------


def test_bottom_center_anchor() -> None:
    d = det(1, 0.5, 0.8)
    assert bottom_center_anchor(d) == (0.5, 0.8)


def test_line_side_signs() -> None:
    # Directed line left->right along y=0.5: above the line is "left" (+).
    assert line_side((0.5, 0.6), (0.0, 0.5), (1.0, 0.5)) > 0
    assert line_side((0.5, 0.4), (0.0, 0.5), (1.0, 0.5)) < 0
    assert line_side((0.5, 0.5), (0.0, 0.5), (1.0, 0.5)) == 0


def test_segments_cross_line_positive() -> None:
    assert segments_cross_line((0.2, 0.3), (0.8, 0.7), (0.0, 0.5), (1.0, 0.5))


def test_segments_cross_line_negative_same_side() -> None:
    assert not segments_cross_line((0.2, 0.3), (0.8, 0.3), (0.0, 0.5), (1.0, 0.5))


def test_segments_cross_line_outside_finite_segment() -> None:
    # Crossing the infinite line beyond the segment's end point.
    assert not segments_cross_line((1.2, 0.3), (1.4, 0.7), (0.0, 0.5), (1.0, 0.5))


def test_point_in_polygon() -> None:
    poly = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
    assert point_in_polygon((0.5, 0.5), poly)
    assert not point_in_polygon((0.05, 0.5), poly)


# --- line crossing -----------------------------------------------------------


def _crossing_rule(**kw) -> LineCrossingRule:
    base = dict(
        id="cross",
        line=LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5)),
        direction="both",
        classes=ClassFilter(include=["person"]),
    )
    base.update(kw)
    return LineCrossingRule(**base)


def test_crossing_counts_one_per_direction() -> None:
    # Track walks left->right then right->left across y=0.5.
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3)]),
        frame(1, 0.1, [det(1, 0.8, 0.7)]),   # crosses to right side
        frame(2, 0.2, [det(1, 0.8, 0.7)]),
        frame(3, 0.3, [det(1, 0.2, 0.3)]),   # crosses back
    ]
    events = run_engine([_crossing_rule()], frames)
    assert len(events) == 2
    assert events[0].payload["from_side"] == -1 and events[0].payload["to_side"] == 1
    assert events[1].payload["from_side"] == 1 and events[1].payload["to_side"] == -1
    assert events[0].source_time_s == pytest.approx(0.1)
    assert events[1].source_time_s == pytest.approx(0.3)


def test_crossing_direction_filter() -> None:
    # Line directed left->right: "left_to_right" counts movement from the
    # start side (side +1, above) to the end side (side -1, below).
    rule = _crossing_rule(direction="left_to_right")
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.7)]),   # start side (+1)
        frame(1, 0.1, [det(1, 0.8, 0.3)]),   # crosses to end side (-1): counts
        frame(2, 0.2, [det(1, 0.8, 0.3)]),
        frame(3, 0.3, [det(1, 0.2, 0.7)]),   # reverse: filtered out
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].payload["from_side"] == 1
    assert events[0].payload["to_side"] == -1


def test_crossing_boundary_jitter_suppressed_by_deadband() -> None:
    # Anchor oscillates within the deadband of the line: no crossing.
    rule = _crossing_rule(deadband=0.2)
    frames = [
        frame(0, 0.0, [det(1, 0.5, 0.45)]),
        frame(1, 0.1, [det(1, 0.5, 0.55)]),  # travel 0.1 < deadband 0.2
        frame(2, 0.2, [det(1, 0.5, 0.45)]),
    ]
    events = run_engine([rule], frames)
    assert events == []


def test_crossing_debounce_requires_stable_side() -> None:
    rule = _crossing_rule(debounce_frames=3)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3)]),
        frame(1, 0.1, [det(1, 0.8, 0.7)]),   # cross, pending=1
        frame(2, 0.2, [det(1, 0.2, 0.3)]),   # back before debounce completes
        frame(3, 0.3, [det(1, 0.8, 0.7)]),   # cross again, pending=1
        frame(4, 0.4, [det(1, 0.8, 0.7)]),   # pending=2
        frame(5, 0.5, [det(1, 0.8, 0.7)]),   # pending=3 -> counts
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(0.5)


def test_crossing_class_filter_excludes_other_classes() -> None:
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3, label="car")]),
        frame(1, 0.1, [det(1, 0.8, 0.7, label="car")]),
    ]
    events = run_engine([_crossing_rule()], frames)
    assert events == []


def test_crossing_untracked_detections_ignored() -> None:
    d = det(None, 0.5, 0.8)  # type: ignore[arg-type]
    frames = [frame(0, 0.0, [d])]
    events = run_engine([_crossing_rule()], frames)
    assert events == []


# --- zone entry --------------------------------------------------------------


ZONE = PolygonGeometry(points=[(0.4, 0.4), (0.9, 0.4), (0.9, 0.9), (0.4, 0.9)])


def _entry_rule(**kw) -> ZoneEntryRule:
    base = dict(id="zone", zone=ZONE, classes=ClassFilter(include=["person"]))
    base.update(kw)
    return ZoneEntryRule(**base)


def test_entry_outside_to_inside() -> None:
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.6)]),   # outside (left of zone)
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # inside -> entry
        frame(2, 0.2, [det(1, 0.6, 0.6)]),
    ]
    events = run_engine([_entry_rule()], frames)
    assert len(events) == 1
    assert events[0].rule_type == "zone_entry"
    assert events[0].source_time_s == pytest.approx(0.1)


def test_first_seen_inside_is_occupancy_not_entry() -> None:
    # Track appears already inside the zone: no entry event (ADR-4).
    frames = [
        frame(0, 0.0, [det(1, 0.6, 0.6)]),
        frame(1, 0.1, [det(1, 0.6, 0.6)]),
    ]
    events = run_engine([_entry_rule()], frames)
    assert events == []


def test_re_entry_after_exit_counts_again() -> None:
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.6)]),   # outside
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # entry #1
        frame(2, 0.2, [det(1, 0.2, 0.6)]),   # exit
        frame(3, 0.3, [det(1, 0.6, 0.6)]),   # entry #2
    ]
    events = run_engine([_entry_rule()], frames)
    assert len(events) == 2
    assert events[0].source_time_s == pytest.approx(0.1)
    assert events[1].source_time_s == pytest.approx(0.3)


def test_entry_debounce() -> None:
    rule = _entry_rule(debounce_frames=2)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.6)]),   # outside
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # inside, pending=1
        frame(2, 0.2, [det(1, 0.2, 0.6)]),   # back outside: cancelled
        frame(3, 0.3, [det(1, 0.6, 0.6)]),   # inside, pending=1
        frame(4, 0.4, [det(1, 0.6, 0.6)]),   # pending=2 -> entry
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(0.4)


# --- dwell -------------------------------------------------------------------


def _dwell_rule(**kw) -> DwellRule:
    base = dict(
        id="dwell", zone=ZONE, classes=ClassFilter(include=["person"]),
        dwell_seconds=1.0, missing_track_grace_s=0.5,
    )
    base.update(kw)
    return DwellRule(**base)


def test_dwell_fires_after_continuous_presence() -> None:
    # 10 fps; inside from t=0 to t=2 => dwell of 1.0s fires at t=1.0.
    frames = [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(21)]
    events = run_engine([_dwell_rule()], frames)
    assert len(events) == 1
    assert events[0].rule_type == "dwell"
    assert events[0].source_time_s == pytest.approx(1.0)
    assert events[0].payload["dwell_seconds"] == pytest.approx(1.0)


def test_dwell_resets_on_exit() -> None:
    # Inside 0.9s, exit, re-enter: never reaches 1.0s continuously.
    frames = (
        [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(9)]      # 0..0.8
        + [frame(9, 0.9, [det(1, 0.2, 0.6)])]                          # exit
        + [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(10, 19)]  # re-enter 0.9..1.8
    )
    events = run_engine([_dwell_rule()], frames)
    assert events == []


def test_dwell_missing_track_grace_resets() -> None:
    # Inside until t=0.4, track missing 0.5..1.0 (absent > grace 0.5 from
    # t=1.0 on), reappears at t=1.1. Dwell must restart from the
    # re-appearance and fire at t=2.1, not continue from t=0.0.
    frames = (
        [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(5)]      # 0..0.4
        + [frame(i, i / FPS, []) for i in range(5, 11)]                # missing 0.5..1.0
        + [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(11, 22)]
    )
    events = run_engine([_dwell_rule()], frames)
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(2.1)


def test_dwell_within_grace_continues() -> None:
    # Track absent for only 0.4s (<= grace 0.5): dwell continues across the
    # gap and fires at t=1.0 (1.0s of presence from t=0.0).
    frames = (
        [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(5)]      # 0..0.4
        + [frame(i, i / FPS, []) for i in range(5, 9)]                 # missing 0.5..0.8
        + [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(9, 21)]
    )
    events = run_engine([_dwell_rule()], frames)
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(1.0)


def test_dwell_sparse_samples_use_source_time() -> None:
    # Samples only every 0.6s (sparse); dwell measured in source seconds,
    # so 1.0s threshold fires at the first sample >= 1.0s of presence.
    frames = [
        frame(0, 0.0, [det(1, 0.6, 0.6)]),
        frame(1, 0.6, [det(1, 0.6, 0.6)]),
        frame(2, 1.2, [det(1, 0.6, 0.6)]),   # duration 1.2 >= 1.0 -> fires
    ]
    events = run_engine([_dwell_rule()], frames)
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(1.2)


# --- sessions, IDs, determinism ----------------------------------------------


def test_event_ids_namespaced_and_stable() -> None:
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3)]),
        frame(1, 0.1, [det(1, 0.8, 0.7)]),
    ]
    events_a = run_engine([_crossing_rule()], frames, camera="cam-01", session="s1")
    events_b = run_engine([_crossing_rule()], frames, camera="cam-01", session="s1")
    assert [e.event_id for e in events_a] == [e.event_id for e in events_b]
    assert events_a[0].event_id.startswith("cam-01/s1/t1/cross/")


def test_separate_sessions_get_distinct_ids() -> None:
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3)]),
        frame(1, 0.1, [det(1, 0.8, 0.7)]),
    ]
    events_a = run_engine([_crossing_rule()], frames, session="s1")
    events_b = run_engine([_crossing_rule()], frames, session="s2")
    assert events_a[0].event_id != events_b[0].event_id


def test_timestamp_discontinuity_resets_state() -> None:
    # A large source-time jump (discontinuity) must reset rule state; the
    # crossing after the jump is evaluated against the post-jump observation.
    engine = RuleEngine([_crossing_rule()], camera_id="cam-01", session_id="s1")
    f0 = frame(0, 0.0, [det(1, 0.2, 0.3)])
    engine.process_frame(f0, emitted_utc="synthetic-0")
    # Simulate discontinuity: caller detects the jump and resets the session.
    engine.reset_session()
    f1 = frame(1, 100.0, [det(1, 0.2, 0.3)])
    f2 = frame(2, 100.1, [det(1, 0.8, 0.7)])
    events = engine.process_frame(f1, emitted_utc="synthetic-1")
    events += engine.process_frame(f2, emitted_utc="synthetic-2")
    # After reset the track is first seen on the left; crossing to right counts.
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(100.1)


def test_deterministic_replay_same_events() -> None:
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3), det(2, 0.6, 0.6)]),
        frame(1, 0.1, [det(1, 0.8, 0.7), det(2, 0.6, 0.6)]),
        frame(2, 0.2, [det(1, 0.8, 0.7), det(2, 0.2, 0.6)]),
    ]
    rules = [_crossing_rule(), _entry_rule()]
    a = run_engine(rules, frames)
    b = run_engine(rules, frames)
    assert [(e.event_id, e.source_time_s) for e in a] == \
           [(e.event_id, e.source_time_s) for e in b]
