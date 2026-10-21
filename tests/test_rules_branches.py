"""Rule-engine branch coverage (deterministic, no GPU).

Targets the state-machine branches that the primary fixtures do not exercise:
crossing debounce confirmation/cancellation on a stable side, the
right-to-left direction filter, zone-entry first-seen-inside occupancy with and
without debounce, dwell re-entry after grace expiry, and the missing-track
grace reset applied to an absent track. Reuses the shared fixture helpers so
the geometry and clock conventions stay identical to the main suite.
"""

from __future__ import annotations

import pytest

from ava.rules import class_matches
from tests.test_rules_engine import (
    FPS,
    _crossing_rule,
    _dwell_rule,
    _entry_rule,
    det,
    frame,
    run_engine,
)

# --- line crossing: debounce confirmation and cancellation -------------------


def test_crossing_debounce_confirms_on_stable_side() -> None:
    # After a crossing starts the pending count, the track stays on the new
    # side (no further segment crosses the line). The debounce branch that
    # counts consecutive stable frames must confirm the crossing.
    rule = _crossing_rule(debounce_frames=3)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.7)]),   # below the line (side +1)
        frame(1, 0.1, [det(1, 0.8, 0.3)]),   # crosses above (side -1); pending=1
        frame(2, 0.2, [det(1, 0.9, 0.35)]),  # moves along the line, no re-cross; pending=2
        frame(3, 0.3, [det(1, 0.95, 0.3)]),  # stays above, no re-cross; pending=3 -> confirms
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(0.3)
    # from_side is the last *confirmed* side (state.side), which only updates
    # on first observation when it was 0; to_side is the pending side reached.
    assert events[0].payload["to_side"] == -1


def test_crossing_debounce_cancelled_by_return_to_line() -> None:
    # A crossing starts the pending count, but the next frame lands exactly on
    # the line (side 0). The stable-side branch sees a different side and must
    # cancel the count; no crossing is emitted.
    rule = _crossing_rule(debounce_frames=2)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3)]),   # left side
        frame(1, 0.1, [det(1, 0.8, 0.7)]),   # crosses to right; pending=1
        frame(2, 0.2, [det(1, 0.5, 0.5)]),   # lands on the line (side 0): cancel
    ]
    events = run_engine([rule], frames)
    assert events == []


# --- line crossing: right-to-left direction filter ---------------------------


def test_crossing_right_to_left_direction_filter() -> None:
    # Line directed left->right: "right_to_left" counts movement from the end
    # side (side -1, below) to the start side (side +1, above). The reverse
    # crossing is filtered out.
    rule = _crossing_rule(direction="right_to_left")
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.3)]),   # above the line (side +1)
        frame(1, 0.1, [det(1, 0.8, 0.7)]),   # crosses below (side -1): counts
        frame(2, 0.2, [det(1, 0.8, 0.7)]),
        frame(3, 0.3, [det(1, 0.2, 0.3)]),   # reverse (+1 -> -1): filtered out
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].payload["from_side"] == -1
    assert events[0].payload["to_side"] == 1


# --- zone entry: first-seen-inside occupancy with debounce -------------------


def test_entry_first_seen_inside_with_debounce_is_occupancy() -> None:
    # A track first observed already inside the zone is occupancy, not an
    # observed entry (ADR-4). With a debounce configured, the first-seen branch
    # arms the pending count but must still emit no entry event.
    rule = _entry_rule(debounce_frames=2)
    frames = [
        frame(0, 0.0, [det(1, 0.6, 0.6)]),   # first seen inside (occupancy)
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # still inside
    ]
    events = run_engine([rule], frames)
    assert events == []


# --- dwell: re-entry after grace expiry --------------------------------------


def test_dwell_reentry_after_grace_expiry_fires_again() -> None:
    # Track dwells, leaves (confirmed exit resets), then re-enters and dwells
    # again. The second continuous presence must fire its own dwell event.
    rule = _dwell_rule(dwell_seconds=0.5, missing_track_grace_s=0.2)
    frames = (
        [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(7)]      # inside 0..0.6 -> fires at 0.5
        + [frame(7, 0.7, [det(1, 0.2, 0.6)])]                          # exit (reset)
        + [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(8, 15)]  # re-enter 0.8..1.4 -> fires at 1.3
    )
    events = run_engine([rule], frames)
    assert len(events) == 2
    assert events[0].source_time_s == pytest.approx(0.5)
    assert events[1].source_time_s == pytest.approx(1.3)


# --- dwell: missing-track grace reset on an absent track ---------------------


def test_dwell_grace_expiry_resets_absent_track() -> None:
    # Track is inside, then disappears for longer than the grace interval. The
    # per-frame expiry pass must reset its dwell so a later re-appearance starts
    # a fresh dwell rather than continuing across the gap.
    rule = _dwell_rule(dwell_seconds=1.0, missing_track_grace_s=0.5)
    frames = (
        [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(5)]      # inside 0..0.4
        + [frame(i, i / FPS, []) for i in range(5, 12)]                # absent 0.5..1.1 (grace expires)
        + [frame(i, i / FPS, [det(1, 0.6, 0.6)]) for i in range(12, 23)]  # reappear 1.2..2.2 -> fires at 2.2
    )
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].source_time_s == pytest.approx(2.2)


# --- class filter: non-empty include match and reject ------------------------


def test_class_filter_empty_include_matches_all() -> None:
    # Empty include list means all classes match (line 109).
    from ava.config import ClassFilter
    rule = _crossing_rule(classes=ClassFilter(include=[]))
    d = det(1, 0.2, 0.3, label="person")
    assert class_matches(d, rule.classes) is True


def test_class_filter_matches_included_label() -> None:
    # Non-empty include list that matches the detection label (line 110).
    from ava.config import ClassFilter
    rule = _crossing_rule(classes=ClassFilter(include=["person"]))
    d = det(1, 0.2, 0.3, label="person")
    assert class_matches(d, rule.classes) is True


def test_class_filter_rejects_excluded_label() -> None:
    # Non-empty include list that does not match the detection label (line 110).
    from ava.config import ClassFilter
    rule = _crossing_rule(classes=ClassFilter(include=["car"]))
    d = det(1, 0.2, 0.3, label="person")
    assert class_matches(d, rule.classes) is False


# --- line crossing: debounce confirm on stable side (pending_side == side) ---


def test_crossing_debounce_confirm_pending_side_equals_side() -> None:
    # Track crosses to a new side and stays there. The pending_side == side
    # branch increments pending_frames until it reaches debounce_frames and
    # confirms the crossing.
    rule = _crossing_rule(debounce_frames=3)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.7)]),   # below line (side +1)
        frame(1, 0.1, [det(1, 0.8, 0.3)]),   # crosses above (side -1); pending=1
        frame(2, 0.2, [det(1, 0.75, 0.35)]), # stays above; pending=2
        frame(3, 0.3, [det(1, 0.7, 0.4)]),   # stays above; pending=3 -> confirms
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].payload["to_side"] == -1


# --- zone entry: confirmed exit (not inside and state.in_zone) ---------------


def test_entry_confirmed_exit_resets_state() -> None:
    # Track enters zone (confirmed), then exits. The confirmed-exit branch
    # resets in_zone and pending counters.
    rule = _entry_rule(debounce_frames=0)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.6)]),   # outside
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # inside -> entry event
        frame(2, 0.2, [det(1, 0.2, 0.6)]),   # exits -> confirmed exit branch
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].rule_type == "zone_entry"


# --- zone entry: jitter cancel (not inside and pending_side != 0) ------------


def test_entry_jitter_cancel_on_brief_exit() -> None:
    # Track enters with debounce, briefly exits before confirm completes.
    # The jitter-cancel branch resets pending_side and pending_frames.
    rule = _entry_rule(debounce_frames=3)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.6)]),   # outside
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # inside; pending=1
        frame(2, 0.2, [det(1, 0.2, 0.6)]),   # exits -> jitter cancel branch
    ]
    events = run_engine([rule], frames)
    assert events == []


# --- zone entry: in-zone pending reset (inside and state.in_zone) ------------


def test_entry_in_zone_pending_reset() -> None:
    # Track is already confirmed inside the zone. The in-zone branch resets
    # pending_side and pending_frames each frame while staying inside.
    rule = _entry_rule(debounce_frames=0)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.6)]),   # outside
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # inside -> entry event
        frame(2, 0.2, [det(1, 0.65, 0.65)]), # still inside -> in-zone reset
        frame(3, 0.3, [det(1, 0.7, 0.7)]),   # still inside -> in-zone reset
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1


def test_entry_in_zone_with_debounce_pending_reset() -> None:
    # Track enters with debounce, stays inside. The in-zone branch (line 422)
    # resets pending counters while the track remains confirmed inside.
    rule = _entry_rule(debounce_frames=2)
    frames = [
        frame(0, 0.0, [det(1, 0.2, 0.6)]),   # outside
        frame(1, 0.1, [det(1, 0.6, 0.6)]),   # inside; pending=1
        frame(2, 0.2, [det(1, 0.65, 0.65)]), # inside; pending=2 -> confirms entry
        frame(3, 0.3, [det(1, 0.7, 0.7)]),   # still inside -> in-zone reset branch
    ]
    events = run_engine([rule], frames)
    assert len(events) == 1
    assert events[0].rule_type == "zone_entry"
