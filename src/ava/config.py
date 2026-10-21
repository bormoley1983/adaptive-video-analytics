"""Run configuration for AVA.

Configuration is data, not code: a single run is described by a YAML file so
behaviour can change without editing Python. The fields cover one input clip,
one detector path and output locations, plus rule profiles (line crossing,
zone entry, dwell) with pre-run validation per ADR-4: normalized geometry,
explicit clocks, bottom-center anchors, and class prompts kept separate from
rules.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Schema version for run configs and rule profiles (ADR-4/ADR-5). Bump when the
# contract changes; events record the version they were produced under.
CONFIG_SCHEMA_VERSION = "1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_id(value: str, field_name: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError(
            f"{field_name} must start with a letter or digit and contain only "
            f"letters, digits, '.', '_' or '-' (got {value!r})"
        )
    return value


class DetectorConfig(BaseModel):
    """Which inference path to run and with what settings."""

    kind: Literal["conventional", "prompted"] = "conventional"
    # Checkpoint file (e.g. models/yolo26n.pt or models/yoloe-26s-seg.pt).
    model: Path
    # For the prompted path: text class prompts. Ignored for conventional.
    # Kept separate from rule profiles on purpose (ADR-4): rules reference
    # class names, they never carry prompts themselves.
    prompts: list[str] = Field(default_factory=list)
    # Text encoder asset for YOLOE (e.g. models/mobileclip2_b.ts).
    text_encoder: Path | None = None
    confidence: float = Field(0.35, gt=0.0, lt=1.0)
    iou: float = Field(0.5, gt=0.0, lt=1.0)


class LineGeometry(BaseModel):
    """A finite line segment in normalized source-image coordinates (0..1).

    Crossing is evaluated against this segment using the track's bottom-center
    anchor (ADR-4). Both endpoints must lie inside the frame and the segment
    must have non-zero length.
    """

    start: tuple[float, float]
    end: tuple[float, float]

    @field_validator("start", "end")
    @classmethod
    def _in_unit_square(cls, point: tuple[float, float]) -> tuple[float, float]:
        # Pydantic coerces the field to a 2-tuple before this runs, so length
        # is guaranteed here; only the unit-square bounds need checking.
        x, y = point
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError(
                f"line endpoint ({x}, {y}) outside normalized [0, 1] frame coordinates"
            )
        return point

    @model_validator(mode="after")
    def _non_degenerate(self) -> LineGeometry:
        if self.start == self.end:
            raise ValueError("line start and end must differ (zero-length line)")
        return self


class PolygonGeometry(BaseModel):
    """A closed polygon in normalized source-image coordinates (0..1).

    Entry is an outside->inside transition of the bottom-center anchor (ADR-4);
    a track first seen inside is occupancy, not observed entry. Requires at
    least 3 distinct vertices, all inside the frame.
    """

    points: list[tuple[float, float]] = Field(min_length=3)

    @field_validator("points")
    @classmethod
    def _valid_points(cls, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        # Pydantic coerces each vertex to a 2-tuple before this runs, so length
        # is guaranteed; only the unit-square bounds need checking per vertex.
        for i, point in enumerate(points):
            x, y = point
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(
                    f"polygon vertex {i} ({x}, {y}) outside normalized [0, 1] frame coordinates"
                )
        distinct = {(p[0], p[1]) for p in points}
        if len(distinct) < 3:
            raise ValueError("polygon needs at least 3 distinct vertices")
        return points


class ClassFilter(BaseModel):
    """Which detected classes a rule applies to.

    Matches detector class labels (e.g. 'person', 'car'). An empty list means
    all classes. Prompts live in DetectorConfig, never here (ADR-4).
    """

    include: list[str] = Field(default_factory=list)


class EvidenceOptions(BaseModel):
    """What evidence to attach to emitted events (ADR-5)."""

    # Save a snapshot frame at the event moment.
    snapshot: bool = False
    # Save a bounded clip around the event.
    clip: bool = False
    # Clip length in source seconds (used when clip is true).
    clip_seconds: float = Field(3.0, gt=0.0)


class LineCrossingRule(BaseModel):
    """Count tracked objects crossing a finite line, per direction (ADR-4).

    Direction is the sign of the anchor's movement across the line; 'both'
    counts crossings in either direction separately. A deadband suppresses
    re-counts while the anchor jitters around the line.
    """

    id: str
    type: Literal["line_crossing"] = "line_crossing"
    line: LineGeometry
    # Which side-to-side movements count: 'both', 'left_to_right', 'right_to_left'.
    direction: Literal["both", "left_to_right", "right_to_left"] = "both"
    classes: ClassFilter = Field(default_factory=ClassFilter)
    # Minimum anchor travel (normalized units) before a crossing is accepted;
    # suppresses boundary jitter.
    deadband: float = Field(0.0, ge=0.0, le=1.0)
    # Frames the anchor must stay on the new side before the crossing counts.
    debounce_frames: int = Field(0, ge=0)
    evidence: EvidenceOptions = Field(default_factory=EvidenceOptions)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_id(value, "rule id")


class ZoneEntryRule(BaseModel):
    """Alert when a tracked object enters a polygon zone (ADR-4)."""

    id: str
    type: Literal["zone_entry"] = "zone_entry"
    zone: PolygonGeometry
    classes: ClassFilter = Field(default_factory=ClassFilter)
    # Frames the anchor must stay inside before entry is confirmed.
    debounce_frames: int = Field(0, ge=0)
    evidence: EvidenceOptions = Field(default_factory=EvidenceOptions)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_id(value, "rule id")


class DwellRule(BaseModel):
    """Alert when a tracked object dwells in a zone for a continuous duration.

    Dwell is measured in source seconds (ADR-4), resets on confirmed exit or
    when the track is missing longer than `missing_track_grace_s`. A track
    first seen inside starts dwell immediately (occupancy).
    """

    id: str
    type: Literal["dwell"] = "dwell"
    zone: PolygonGeometry
    classes: ClassFilter = Field(default_factory=ClassFilter)
    # Continuous presence required, in source seconds.
    dwell_seconds: float = Field(gt=0.0)
    # A track absent for longer than this (source seconds) resets its dwell.
    missing_track_grace_s: float = Field(2.0, ge=0.0)
    evidence: EvidenceOptions = Field(default_factory=EvidenceOptions)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_id(value, "rule id")


Rule = LineCrossingRule | ZoneEntryRule | DwellRule


class RuleProfile(BaseModel):
    """A named bundle of rules applied to one camera (ADR-4).

    Profiles are referenced by name from the run config; unresolved references
    are rejected before a run starts.
    """

    id: str
    # Human-readable description, optional.
    description: str | None = None
    rules: list[Rule] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_id(value, "profile id")

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> RuleProfile:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r} in profile {self.id!r}")
            seen.add(rule.id)
        return self


class CameraConfig(BaseModel):
    """One camera's input and the rule profiles applied to it."""

    id: str
    # Input clip for replay; live stream sources are not supported yet.
    source: Path
    # Rule profile names that must resolve against `profiles` in RunConfig.
    profiles: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_id(value, "camera id")


class RunConfig(BaseModel):
    """A single AVA run over one or more camera inputs."""

    # Schema version recorded in events (ADR-5).
    schema_version: str = CONFIG_SCHEMA_VERSION
    source: Path | None = None
    detector: DetectorConfig | None = None
    output_dir: Path = Path("results")
    # Explicit GPU selection (index or UUID). None => let the runtime decide.
    gpu: str | None = "0"
    # Persist per-frame detection records as JSONL.
    jsonl: bool = True
    # Write an annotated MP4 alongside the JSONL.
    annotate: bool = True

    # Rule-driven camera configuration (ADR-4).
    cameras: list[CameraConfig] = Field(default_factory=list)
    profiles: list[RuleProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _resolve_references(self) -> RunConfig:
        # Simple runs (single source + detector) remain valid.
        if self.source is None and not self.cameras:
            raise ValueError(
                "config must define either `source`+`detector` "
                "or at least one entry in `cameras`"
            )
        if self.cameras and self.detector is None:
            raise ValueError("camera runs require a `detector` section")

        profile_ids = {p.id for p in self.profiles}
        camera_ids = [c.id for c in self.cameras]
        if len(camera_ids) != len(set(camera_ids)):
            dupes = sorted({c for c in camera_ids if camera_ids.count(c) > 1})
            raise ValueError(f"duplicate camera ids: {', '.join(dupes)}")

        for cam in self.cameras:
            for ref in cam.profiles:
                if ref not in profile_ids:
                    raise ValueError(
                        f"camera {cam.id!r} references unknown rule profile {ref!r}; "
                        f"defined profiles: {sorted(profile_ids) or 'none'}"
                    )
        return self


def load_config(path: Path) -> RunConfig:
    """Load and validate a run configuration from YAML.

    Validation failures (bad geometry, thresholds, unresolved profile refs)
    raise before any processing starts (ADR-4).
    """
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return RunConfig.model_validate(raw)
