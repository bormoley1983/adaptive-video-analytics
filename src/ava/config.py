"""Run configuration for AVA.

Configuration is data, not code: a single run is described by a YAML file so
behaviour can change without editing Python. The fields cover one input clip,
one detector path and output locations, plus rule profiles (line crossing,
zone entry, dwell) with pre-run validation per ADR-4: normalized geometry,
explicit clocks, bottom-center anchors, and class prompts kept separate from
rules.
"""

from __future__ import annotations

import os
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
    least 3 distinct vertices, all inside the frame, non-zero area and no
    self-intersection.
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

    @model_validator(mode="after")
    def _non_degenerate_polygon(self) -> PolygonGeometry:
        # Reject zero-area (collinear) polygons.
        area = _polygon_area(self.points)
        if abs(area) < 1e-12:
            raise ValueError("polygon has zero area (vertices are collinear)")
        # Reject self-intersecting polygons.
        if _polygon_self_intersects(self.points):
            raise ValueError("polygon edges intersect (self-intersecting polygon)")
        return self


def _polygon_area(points: list[tuple[float, float]]) -> float:
    """Shoelace formula for signed area of a polygon."""
    n = len(points)
    area = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _segments_intersect(
    p1: tuple[float, float], p2: tuple[float, float],
    p3: tuple[float, float], p4: tuple[float, float],
) -> bool:
    """True if segment p1-p2 properly intersects segment p3-p4.

    Uses the standard orientation test. Collinear overlapping segments are
    also detected via on-segment checks.
    """
    def orient(a, b, c):
        val = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
        if abs(val) < 1e-12:
            return 0
        return 1 if val > 0 else 2

    def on_segment(a, b, c):
        # True if b lies on segment a-c (assuming collinear).
        return (min(a[0], c[0]) - 1e-12 <= b[0] <= max(a[0], c[0]) + 1e-12 and
                min(a[1], c[1]) - 1e-12 <= b[1] <= max(a[1], c[1]) + 1e-12)

    o1 = orient(p1, p2, p3)
    o2 = orient(p1, p2, p4)
    o3 = orient(p3, p4, p1)
    o4 = orient(p3, p4, p2)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_segment(p1, p3, p2):
        return True
    if o2 == 0 and on_segment(p1, p4, p2):
        return True
    if o3 == 0 and on_segment(p3, p1, p4):
        return True
    return bool(o4 == 0 and on_segment(p3, p2, p4))


def _polygon_self_intersects(points: list[tuple[float, float]]) -> bool:
    """Check if any non-adjacent edges of the polygon intersect."""
    n = len(points)
    edges = [(points[i], points[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 2, n):
            # Skip adjacent edges (they share a vertex).
            if i == 0 and j == n - 1:
                continue
            if _segments_intersect(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                return True
    return False


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


class TelemetryConfig(BaseModel):
    """Optional telemetry surface for a live run (B-021).

    Structured JSON logs on stdout are always on; the Prometheus scrape
    endpoint is opt-in so runtime progress never depends on a collector.
    """

    # Bind a local /metrics endpoint when set (loopback only by default).
    prometheus_port: int | None = Field(None, ge=1, le=65535)
    # Interface the scrape endpoint binds to. Loopback keeps the study stack off
    # the LAN; set "0.0.0.0" when a container (e.g. Prometheus via host-gateway)
    # must reach it through the host's bridge IP.
    prometheus_host: str = "127.0.0.1"
    # Minimum severity emitted to stdout: debug < info < warning < error.
    log_level: Literal["debug", "info", "warning", "error"] = "info"


class LiveCameraConfig(BaseModel):
    """One live RTSP camera and the rule profiles applied to it."""

    id: str
    # RTSP URL (e.g. rtsp://127.0.0.1:8554/cam-01).
    url: str
    # Rule profile names that must resolve against `profiles` in RunConfig.
    profiles: list[str] = Field(default_factory=list)
    # Target inference rate in frames per second. Decode runs at native rate;
    # inference is decimated to this rate.
    analysis_fps: float = Field(4.0, gt=0.0)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_id(value, "camera id")

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        if not value.startswith(("rtsp://", "rtsps://")):
            raise ValueError(f"live camera url must start with rtsp:// or rtsps:// (got {value!r})")
        return value


class LiveRunConfig(BaseModel):
    """Configuration for a live multi-camera RTSP run."""

    # Decode backend: "opencv" (simplest) or "gstreamer" (explicit buffer caps,
    # NVDEC path later).
    backend: Literal["opencv", "gstreamer"] = "opencv"
    # Detector sharing mode: "shared" (one model, serialized inference) or
    # "per_camera" (one model per camera, parallel inference).
    detector_mode: Literal["shared", "per_camera"] = "shared"
    # Per-camera frame buffer size (frames held for evidence / recent history).
    buffer_size: int = Field(30, ge=1)
    # Maximum reconnect attempts per camera before giving up.
    reconnect_max: int = Field(3, ge=0)
    # Base delay in seconds for exponential backoff on reconnect.
    reconnect_delay_s: float = Field(1.0, gt=0.0)
    # Artificial per-event sink delay in seconds (overload testing only).
    slow_sink_s: float = Field(0.0, ge=0.0)
    # Metrics sampling interval in seconds for host/GPU resource polling.
    metrics_interval_s: float = Field(5.0, gt=0.0)
    # Bounded run duration in seconds. When set (> 0), all cameras are stopped
    # after this wall-clock window so the run returns and writes its summary;
    # when 0 (default) the run is unbounded and stops only on stream end or an
    # external stop signal.
    duration_s: float = Field(0.0, ge=0.0)
    # Optional structured-log / Prometheus surface (B-021).
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    # Live cameras.
    cameras: list[LiveCameraConfig] = Field(default_factory=list)
    # Rule profiles (shared with file-based runs).
    profiles: list[RuleProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _resolve_references(self) -> LiveRunConfig:
        if not self.cameras:
            raise ValueError("live run config requires at least one camera")
        profile_ids = [p.id for p in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            dupes = sorted({p for p in profile_ids if profile_ids.count(p) > 1})
            raise ValueError(f"duplicate rule profile ids: {', '.join(dupes)}")
        camera_ids = [c.id for c in self.cameras]
        if len(camera_ids) != len(set(camera_ids)):
            dupes = sorted({c for c in camera_ids if camera_ids.count(c) > 1})
            raise ValueError(f"duplicate camera ids: {', '.join(dupes)}")
        profile_id_set = set(profile_ids)
        for cam in self.cameras:
            for ref in cam.profiles:
                if ref not in profile_id_set:
                    raise ValueError(
                        f"camera {cam.id!r} references unknown rule profile {ref!r}; "
                        f"defined profiles: {sorted(profile_id_set) or 'none'}"
                    )
        return self


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

        profile_ids = [p.id for p in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            dupes = sorted({p for p in profile_ids if profile_ids.count(p) > 1})
            raise ValueError(f"duplicate rule profile ids: {', '.join(dupes)}")
        camera_ids = [c.id for c in self.cameras]
        if len(camera_ids) != len(set(camera_ids)):
            dupes = sorted({c for c in camera_ids if camera_ids.count(c) > 1})
            raise ValueError(f"duplicate camera ids: {', '.join(dupes)}")

        profile_id_set = set(profile_ids)
        for cam in self.cameras:
            for ref in cam.profiles:
                if ref not in profile_id_set:
                    raise ValueError(
                        f"camera {cam.id!r} references unknown rule profile {ref!r}; "
                        f"defined profiles: {sorted(profile_id_set) or 'none'}"
                    )
        return self


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve_env_refs(value: object) -> object:
    """Resolve ``${VAR}`` / ``${VAR:-default}`` references from the environment.

    A reference is replaced by the variable's value, or by its default when the
    variable is unset (or empty). Strings with no reference are returned as-is;
    containers are resolved recursively so any config value can be env-driven.
    """
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            return os.environ.get(var) or (default if default is not None else "")

        return _ENV_REF.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs(v) for v in value]
    return value


def load_config(path: Path) -> RunConfig:
    """Load and validate a run configuration from YAML.

    Environment references (``${VAR}`` / ``${VAR:-default}``) are resolved at
    load time so values can come from the environment with safe defaults.
    Validation failures (bad geometry, thresholds, unresolved profile refs)
    raise before any processing starts (ADR-4).
    """
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return RunConfig.model_validate(_resolve_env_refs(raw))


def load_live_config(path: Path) -> LiveRunConfig:
    """Load and validate a live RTSP run configuration from YAML.

    The file must contain a `live` top-level key whose value maps to
    :class:`LiveRunConfig`. Detector settings are read from the sibling
    `detector` key (same schema as file-based runs). Environment references
    are resolved at load time (see :func:`_resolve_env_refs`).
    """
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if "live" not in raw:
        raise ValueError(
            f"config {path} does not contain a 'live' section; "
            "use load_config() for file-based runs"
        )
    return LiveRunConfig.model_validate(_resolve_env_refs(raw["live"]))
