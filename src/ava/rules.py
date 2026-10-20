"""Rule engine (ADR-4 semantics).

Turns per-frame detections into line crossings, zone entries and dwell alerts.
Semantics implemented here:

- Bottom-center anchor: the track position is the bounding-box bottom center,
  in normalized [0, 1] source-image coordinates.
- Source time: all rule timing uses the frame's source timestamp (PTS), never
  wall-clock or frame counts, so sampling-rate changes do not alter semantics.
- Crossing: a tracked segment between two consecutive observations crosses the
  configured finite line; direction is derived from the signed side change.
  A deadband (normalized units of travel along the line normal) plus a
  debounce (frames to stay on the new side) suppress boundary jitter.
- Zone entry: an outside -> inside transition of the anchor. A track first
  seen inside is *occupancy*, not observed entry (ADR-4).
- Dwell: continuous presence in source seconds, reset on confirmed exit or
  when the track is missing longer than the configured grace interval. No
  continuity is inferred across reconnects or tracker identity resets.

The engine is deterministic and pure: given the same frame stream it always
produces the same events, which makes fixture testing straightforward.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .config import (
    ClassFilter,
    DwellRule,
    LineCrossingRule,
    Rule,
    ZoneEntryRule,
)
from .detector import Detection, FrameResult


# ---------------------------------------------------------------------------
# Geometry helpers (normalized coordinates)
# ---------------------------------------------------------------------------


def bottom_center_anchor(det: Detection) -> tuple[float, float]:
    """Bottom-center anchor of a detection box, normalized (ADR-4)."""
    x1, y1, x2, y2 = det.box
    return ((x1 + x2) / 2.0, y2)


def line_side(point: tuple[float, float], start: tuple[float, float],
              end: tuple[float, float]) -> float:
    """Signed side of `point` relative to the directed line start->end.

    Positive on the left of the direction vector, negative on the right, zero
    on the line. The magnitude is the perpendicular distance in normalized
    units (not scaled by segment length), which is what the deadband uses.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    return dx * (point[1] - start[1]) - dy * (point[0] - start[0])


def _segment_parameter(point: tuple[float, float], start: tuple[float, float],
                       end: tuple[float, float]) -> float | None:
    """Parameter t in [0, 1] of the closest point on the segment, or None.

    Returns the clamped projection parameter of `point` onto the infinite line
    through the segment; callers check whether the crossing point falls within
    the finite segment via the returned unclamped value.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return None
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq
    return t


def segments_cross_line(
    p0: tuple[float, float],
    p1: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    """True if the segment p0->p1 properly crosses the finite line.

    The endpoints must lie on strictly opposite sides and the intersection
    must fall within the finite segment (parameter in [0, 1] on both
    segments). Touching an endpoint (side == 0) is not a crossing;
    debounce/deadband handle near-line jitter.
    """
    s0 = line_side(p0, start, end)
    s1 = line_side(p1, start, end)
    if s0 == 0.0 or s1 == 0.0 or (s0 > 0) == (s1 > 0):
        return False
    # Standard segment intersection: the two segments p0->p1 and start->end
    # cross iff each pair of endpoints straddles the other's line.
    d1 = line_side(p0, start, end)
    d2 = line_side(p1, start, end)
    d3 = line_side(start, p0, p1)
    d4 = line_side(end, p0, p1)
    return ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
           ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0))


def point_in_polygon(point: tuple[float, float], points: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test (normalized coordinates)."""
    inside = False
    n = len(points)
    j = n - 1
    x, y = point
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def class_matches(det: Detection, classes: ClassFilter) -> bool:
    """A rule applies to a detection if the filter is empty or includes the label."""
    if not classes.include:
        return True
    return det.label in classes.include


# ---------------------------------------------------------------------------
# Track state
# ---------------------------------------------------------------------------


@dataclass
class _TrackState:
    """Per-track, per-rule state. Reset on track loss beyond grace (dwell) or
    on identity reset; never carried across reconnects (ADR-4)."""

    last_anchor: tuple[float, float] | None = None
    last_time_s: float | None = None
    last_frame: int | None = None
    # Line crossing: which side of the line the anchor last settled on.
    side: int = 0  # -1, 0 (unseen/on-line), +1
    pending_side: int = 0
    pending_frames: int = 0
    # Zone entry / dwell.
    in_zone: bool | None = None  # None => first observation not yet recorded
    seen_before: bool = False
    dwell_start_s: float | None = None
    last_seen_time_s: float | None = None


# ---------------------------------------------------------------------------
# Events (internal emission shape, already carrying the ADR-5 minimum fields;
# the full versioned contract lives in ava.events)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleEvent:
    """A rule-triggered event. Field names follow the ADR-5 minimum contract."""

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
    # Emitted wall-clock UTC (ISO-8601); synthetic for replay.
    emitted_utc: str
    # Frame index of the triggering frame; used to locate evidence.
    frame_index: int = -1
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


class RuleEngine:
    """Applies a set of rules to a frame stream and emits events.

    Deterministic: identical input frames produce identical events. Event IDs
    are derived from (camera, session, track, rule, event kind, occurrence) so
    retries over the same stream preserve the same IDs (ADR-5).
    """

    def __init__(self, rules: Iterable[Rule], camera_id: str, session_id: str,
                 schema_version: str = "1",
                 discontinuity_gap_s: float = 2.0) -> None:
        self.rules = list(rules)
        self.camera_id = camera_id
        self.session_id = session_id
        self.schema_version = schema_version
        # A source-time jump beyond this (or any backwards jump) is a
        # discontinuity: new session, all rule state reset (ADR-4).
        self.discontinuity_gap_s = discontinuity_gap_s
        # (rule_id, track_id) -> state
        self._state: dict[tuple[str, int], _TrackState] = {}
        # Per-rule occurrence counters for stable event IDs.
        self._occurrences: dict[tuple[str, int, str], int] = {}
        self._last_frame_time: float | None = None

    # -- public API ---------------------------------------------------------

    def process_frame(self, frame: FrameResult, emitted_utc: str) -> list[RuleEvent]:
        """Process one frame; returns events triggered by this frame.

        A timestamp discontinuity (current PTS earlier than the previous
        frame's, or a jump beyond ``discontinuity_gap_s``) starts a new
        session and resets all rule state (ADR-4).
        """
        if self._last_frame_time is not None:
            gap = frame.timestamp_s - self._last_frame_time
            if gap < 0 or gap > self.discontinuity_gap_s:
                self.reset_session()
        self._last_frame_time = frame.timestamp_s

        events: list[RuleEvent] = []
        observed_tracks: set[int] = set()

        for det in frame.detections:
            if det.track_id is None:
                continue  # untracked detections cannot drive temporal rules
            observed_tracks.add(det.track_id)
            anchor = bottom_center_anchor(det)
            for rule in self.rules:
                if not class_matches(det, rule.classes):
                    continue
                state = self._state_for(rule.id, det.track_id)
                events.extend(
                    self._update_rule(rule, state, det, anchor, frame, emitted_utc)
                )

        # Dwell rules must observe track *loss* to apply the grace interval.
        for rule in self.rules:
            if not isinstance(rule, DwellRule):
                continue
            events.extend(
                self._expire_dwell(rule, observed_tracks, frame.timestamp_s, emitted_utc)
            )
        return events

    def reset_session(self) -> None:
        """Discontinuity starts a new session and resets all rule state (ADR-4)."""
        self._state.clear()
        self._occurrences.clear()
        self._session_discontinuous = True

    # -- internals ----------------------------------------------------------

    def _state_for(self, rule_id: str, track_id: int) -> _TrackState:
        key = (rule_id, track_id)
        state = self._state.get(key)
        if state is None:
            state = _TrackState()
            self._state[key] = state
        return state

    def _next_event_id(self, rule_id: str, track_id: int, kind: str) -> str:
        key = (rule_id, track_id, kind)
        n = self._occurrences.get(key, 0)
        self._occurrences[key] = n + 1
        return f"{self.camera_id}/{self.session_id}/t{track_id}/{rule_id}/{kind}-{n}"

    def _emit(self, rule: Rule, track_id: int, kind: str, det: Detection,
              source_time_s: float, emitted_utc: str,
              payload: dict | None = None, frame_index: int = -1) -> RuleEvent:
        return RuleEvent(
            schema_version=self.schema_version,
            event_id=self._next_event_id(rule.id, track_id, kind),
            camera_id=self.camera_id,
            session_id=self.session_id,
            track_id=track_id,
            rule_id=rule.id,
            rule_type=rule.type,
            class_label=det.label,
            confidence=det.confidence,
            source_time_s=source_time_s,
            emitted_utc=emitted_utc,
            frame_index=frame_index,
            payload=payload or {},
        )

    def _update_rule(self, rule: Rule, state: _TrackState, det: Detection,
                     anchor: tuple[float, float], frame: FrameResult,
                     emitted_utc: str) -> list[RuleEvent]:
        if isinstance(rule, LineCrossingRule):
            return self._update_crossing(rule, state, det, anchor, frame, emitted_utc)
        if isinstance(rule, ZoneEntryRule):
            return self._update_entry(rule, state, det, anchor, frame, emitted_utc)
        if isinstance(rule, DwellRule):
            return self._update_dwell(rule, state, det, anchor, frame, emitted_utc)
        return []

    # -- line crossing ------------------------------------------------------

    def _update_crossing(self, rule: LineCrossingRule, state: _TrackState,
                         det: Detection, anchor: tuple[float, float],
                         frame: FrameResult, emitted_utc: str) -> list[RuleEvent]:
        events: list[RuleEvent] = []
        side_value = line_side(anchor, rule.line.start, rule.line.end)
        side = 1 if side_value > 0 else (-1 if side_value < 0 else 0)

        crossed = False
        if state.last_anchor is not None:
            crossed = segments_cross_line(
                state.last_anchor, anchor, rule.line.start, rule.line.end
            )
        if crossed and self._direction_allowed(rule, state.side, side):
            # Deadband: require travel along the normal beyond the band.
            travel = abs(side_value)
            if travel >= rule.deadband:
                if rule.debounce_frames > 0:
                    if state.pending_side == side:
                        state.pending_frames += 1
                    else:
                        state.pending_side = side
                        state.pending_frames = 1
                    if state.pending_frames >= rule.debounce_frames:
                        events.append(self._emit(
                            rule, det.track_id, "crossing", det,
                            frame.timestamp_s, emitted_utc,
                            {"from_side": state.side, "to_side": side},
                            frame_index=frame.index,
                        ))
                        state.side = side
                        state.pending_side = 0
                        state.pending_frames = 0
                else:
                    events.append(self._emit(
                        rule, det.track_id, "crossing", det,
                        frame.timestamp_s, emitted_utc,
                        {"from_side": state.side, "to_side": side},
                        frame_index=frame.index,
                    ))
                    state.side = side
        elif state.pending_side != 0:
            # No new crossing this frame. Debounce counts consecutive frames
            # stable on the pending side; any move back to a different side
            # (or onto the line) cancels the count.
            if side == state.pending_side:
                state.pending_frames += 1
                if state.pending_frames >= rule.debounce_frames:
                    events.append(self._emit(
                        rule, det.track_id, "crossing", det,
                        frame.timestamp_s, emitted_utc,
                        {"from_side": state.side, "to_side": side},
                        frame_index=frame.index,
                    ))
                    state.side = side
                    state.pending_side = 0
                    state.pending_frames = 0
            else:
                state.pending_side = 0
                state.pending_frames = 0

        if side != 0 and state.side == 0:
            state.side = side
        state.last_anchor = anchor
        state.last_time_s = frame.timestamp_s
        state.last_frame = frame.index
        return events

    @staticmethod
    def _direction_allowed(rule: LineCrossingRule, from_side: int, to_side: int) -> bool:
        if rule.direction == "both":
            return True
        # For a line directed left->right (start on the left), side +1 is the
        # upper/left half-plane and side -1 the lower/right half-plane.
        # left_to_right = anchor moves from the start side (+1) to the end
        # side (-1); right_to_left is the reverse.
        if rule.direction == "left_to_right":
            return from_side == 1 and to_side == -1
        return from_side == -1 and to_side == 1

    # -- zone entry ---------------------------------------------------------

    def _update_entry(self, rule: ZoneEntryRule, state: _TrackState,
                      det: Detection, anchor: tuple[float, float],
                      frame: FrameResult, emitted_utc: str) -> list[RuleEvent]:
        inside = point_in_polygon(anchor, rule.zone.points)
        events: list[RuleEvent] = []

        if state.in_zone is None:
            # First observation: occupancy if inside, but NOT observed entry.
            state.in_zone = inside
            state.seen_before = True
            if inside and rule.debounce_frames == 0:
                state.pending_side = 1
                state.pending_frames = 1
            elif inside:
                state.pending_side = 1
                state.pending_frames = 1
        elif inside and not state.in_zone:
            # Outside -> inside transition.
            if rule.debounce_frames > 0:
                if state.pending_side == 1:
                    state.pending_frames += 1
                else:
                    state.pending_side = 1
                    state.pending_frames = 1
                if state.pending_frames >= rule.debounce_frames:
                    events.append(self._emit(
                        rule, det.track_id, "entry", det,
                        frame.timestamp_s, emitted_utc,
                        frame_index=frame.index,
                    ))
                    state.in_zone = True
                    state.pending_side = 0
                    state.pending_frames = 0
            else:
                events.append(self._emit(
                    rule, det.track_id, "entry", det,
                    frame.timestamp_s, emitted_utc,
                    frame_index=frame.index,
                ))
                state.in_zone = True
        elif not inside and state.in_zone:
            # Confirmed exit.
            state.in_zone = False
            state.pending_side = 0
            state.pending_frames = 0
        elif not inside and state.pending_side != 0:
            # Left the zone before debounce completed: cancel (jitter).
            state.pending_side = 0
            state.pending_frames = 0
        elif inside and state.in_zone:
            state.pending_side = 0
            state.pending_frames = 0

        state.last_anchor = anchor
        state.last_time_s = frame.timestamp_s
        state.last_frame = frame.index
        return events

    # -- dwell --------------------------------------------------------------

    def _update_dwell(self, rule: DwellRule, state: _TrackState,
                      det: Detection, anchor: tuple[float, float],
                      frame: FrameResult, emitted_utc: str) -> list[RuleEvent]:
        inside = point_in_polygon(anchor, rule.zone.points)
        events: list[RuleEvent] = []
        now = frame.timestamp_s

        if state.last_seen_time_s is not None:
            gap = now - state.last_seen_time_s
            # Missing track beyond grace resets dwell (ADR-4: no continuity
            # across reconnects).
            if gap > rule.missing_track_grace_s:
                state.dwell_start_s = None
                state.seen_before = False

        if inside:
            if state.dwell_start_s is None:
                # First seen inside (occupancy) or re-entry after reset.
                state.dwell_start_s = now
                state.seen_before = False
            duration = now - state.dwell_start_s
            if duration >= rule.dwell_seconds and not state.seen_before:
                events.append(self._emit(
                    rule, det.track_id, "dwell", det,
                    frame.timestamp_s, emitted_utc,
                    {"dwell_seconds": round(duration, 6),
                     "dwell_start_s": state.dwell_start_s},
                    frame_index=frame.index,
                ))
                # One dwell event per continuous presence; further frames
                # inside extend the stay without re-emitting until the track
                # leaves (exit or grace expiry) and re-enters.
                state.seen_before = True
        else:
            # Confirmed exit resets continuous dwell.
            state.dwell_start_s = None
            state.seen_before = False

        state.last_anchor = anchor
        state.last_time_s = now
        state.last_frame = frame.index
        state.in_zone = inside
        return events

    def _expire_dwell(self, rule: DwellRule, observed_tracks: set[int],
                      now_s: float, emitted_utc: str) -> list[RuleEvent]:
        """Apply missing-track grace to tracks absent from this frame.

        A track that was inside the zone and is now absent keeps its dwell
        running for up to `missing_track_grace_s` source seconds; beyond that
        the dwell resets (ADR-4: no continuity across reconnects).
        """
        events: list[RuleEvent] = []
        for (rule_id, track_id), state in self._state.items():
            if rule_id != rule.id or track_id in observed_tracks:
                continue
            if state.last_time_s is None or not state.in_zone:
                continue
            absent_for = now_s - state.last_time_s
            if absent_for > rule.missing_track_grace_s:
                # Grace expired: dwell resets; no event (the track vanished).
                state.dwell_start_s = None
                state.seen_before = False
                state.in_zone = False
        return events
