"""AVA API common types and OpenAPI schema loading.

This module provides Pydantic models that mirror the checked-in ``openapi.yaml``
document. The YAML is the wire contract; these models are the Python-side
mirror used by later F-1 stories (S2+). If they diverge, the YAML wins.

No FastAPI, no routes — schema-only. FastAPI arrives with F-1-S2.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, Field

# ── Versioning ────────────────────────────────────────────────────────────────


class SchemaVersion(str, Enum):
    """Contract schema version embedded in every resource."""

    V1_0 = "1.0"


# ── Opaque identifiers ────────────────────────────────────────────────────────

_ULID_BODY = r"[0-9A-HJKMNP-TV-Z]{26}"


def _id_type(prefix: str) -> type[str]:
    """Build a validated string type for a prefixed ULID identifier."""
    import re

    pattern = rf"^{prefix}_{_ULID_BODY}$"

    def _check(v: Any) -> str:
        if not isinstance(v, str) or not re.match(pattern, v):
            raise ValueError(f"expected {prefix}_ ULID, got {v!r}")
        return v

    return Annotated[str, Field(pattern=pattern)]  # type: ignore[type-arg]


# Type aliases for each identifier kind (validated by Pydantic pattern).
EventId = Annotated[str, Field(pattern=rf"^evt_{_ULID_BODY}$")]
CameraId = Annotated[str, Field(pattern=rf"^cam_{_ULID_BODY}$")]
SiteId = Annotated[str, Field(pattern=rf"^site_{_ULID_BODY}$")]
SessionId = Annotated[str, Field(pattern=rf"^ses_{_ULID_BODY}$")]
EvidenceId = Annotated[str, Field(pattern=rf"^evd_{_ULID_BODY}$")]
TenantId = Annotated[str, Field(pattern=rf"^ten_{_ULID_BODY}$")]
JobId = Annotated[str, Field(pattern=rf"^job_{_ULID_BODY}$")]
MessageId = Annotated[str, Field(pattern=rf"^msg_{_ULID_BODY}$")]
ProfileId = Annotated[str, Field(pattern=rf"^prof_{_ULID_BODY}$")]
IncidentId = Annotated[str, Field(pattern=rf"^inc_{_ULID_BODY}$")]
MediaAssetId = Annotated[str, Field(pattern=rf"^med_{_ULID_BODY}$")]
SearchId = Annotated[str, Field(pattern=rf"^srch_{_ULID_BODY}$")]
DeliverySubscriptionId = Annotated[str, Field(pattern=rf"^dlv_{_ULID_BODY}$")]
WorkerId = Annotated[str, Field(pattern=rf"^wrk_{_ULID_BODY}$")]


# ── Time ──────────────────────────────────────────────────────────────────────


class ClockKind(str, Enum):
    """How the UTC source-time value was derived."""

    CAMERA_UTC = "camera_utc"
    FILE_PTS_ONLY = "file_pts_only"
    REPLAY_SYNTHETIC = "replay_synthetic"
    LIVE_EPOCH = "live_epoch"


class SourceTime(BaseModel):
    """Source-clock time for an event.

    Records clock origin, uncertainty, and whether the UTC value is synthetic.
    """

    offset_seconds: float = Field(ge=0)
    captured_at: str | None = None
    clock_kind: ClockKind | None = None
    uncertainty_ms: int | None = Field(default=None, ge=0)
    synthetic: bool | None = None


# ── Geometry (normalized to source image) ─────────────────────────────────────


class NormalizedPoint(BaseModel):
    """A point in normalized source-image coordinates (0..1)."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class NormalizedBox(BaseModel):
    """Axis-aligned box [x1, y1, x2, y2] in (0,0)-(1,1) space."""

    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)


class GeometryKind(str, Enum):
    BOX = "box"
    POLYGON = "polygon"


class Geometry(BaseModel):
    """Normalized geometry in source-image coordinates."""

    kind: GeometryKind
    coordinates: list[float | NormalizedPoint]

    def model_post_init(self, /, __context: Any) -> None:
        if self.kind == GeometryKind.BOX:
            if len(self.coordinates) != 4:
                raise ValueError("box geometry requires exactly 4 coordinates")
            for v in self.coordinates:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise TypeError(f"box coordinate must be a number, got {v!r}")
                if not (0.0 <= float(v) <= 1.0):
                    raise ValueError(f"box coordinate {v} out of range [0, 1]")
        elif self.kind == GeometryKind.POLYGON:
            if len(self.coordinates) < 3:
                raise ValueError("polygon geometry requires at least 3 points")
            for p in self.coordinates:
                if not isinstance(p, NormalizedPoint):
                    raise TypeError(f"polygon coordinate must be a point, got {p!r}")


# ── Confidence ────────────────────────────────────────────────────────────────


class ConfidenceKind(str, Enum):
    DETECTOR = "detector"
    RULE = "rule"
    VERIFIER = "verifier"
    SEARCH_SIMILARITY = "search_similarity"


class Confidence(BaseModel):
    """A confidence score with its kind.

    Different kinds must not be compared as calibrated probabilities.
    """

    value: float = Field(ge=0.0, le=1.0)
    kind: ConfidenceKind


# ── Cursor pagination ─────────────────────────────────────────────────────────


class PageQuery(BaseModel):
    """Cursor-based pagination query parameters."""

    page_cursor: str | None = Field(default=None, alias="page[cursor]")
    page_size: int = Field(default=50, ge=1, le=200, alias="page[size]")


class PageResponse(BaseModel):
    """Cursor-based pagination response envelope."""

    items: list[Any]
    next_cursor: str | None = None


# ── Job state machine ─────────────────────────────────────────────────────────


class JobState(str, Enum):
    """Asynchronous job lifecycle state."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class JobFailure(BaseModel):
    """Structured failure details for a job."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class Job(BaseModel):
    """Asynchronous job resource."""

    id: JobId
    state: JobState
    created_at: str
    updated_at: str
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    failure: JobFailure | None = None
    retry_of: str | None = None


# ── RFC 9457 problem details ──────────────────────────────────────────────────


class FieldError(BaseModel):
    """A single field-level validation error."""

    field: str
    message: str


class ProblemDetail(BaseModel):
    """RFC 9457 problem details.

    No credential, filesystem path, RTSP URL or vendor response body is exposed.
    """

    type: str
    title: str
    status: int = Field(ge=100, le=599)
    code: str
    detail: str | None = None
    request_id: str | None = None
    errors: list[FieldError] | None = None


# ── Resource envelope (common to all resources) ───────────────────────────────


class ResourceEnvelope(BaseModel):
    """Fields present on every AVA resource."""

    schema_version: SchemaVersion
    id: str
    created_at: str
    updated_at: str | None = None
    revision: int | None = Field(default=None, ge=0)


# ── External binding ──────────────────────────────────────────────────────────


class Binding(BaseModel):
    """External system reference.

    External IDs are not AVA IDs and never determine authorization or tenancy.
    """

    system: str
    kind: str
    external_id: str


# ── Schema loading ────────────────────────────────────────────────────────────

_OPENAPI_PATH = Path(__file__).resolve().parent.parent.parent.parent / "openapi.yaml"


def load_schema(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the checked-in OpenAPI 3.1 document.

    Returns the parsed YAML as a dict. Raises ``ValueError`` if the file is
    missing, not valid YAML, or not an OpenAPI 3.1 document.
    """
    p = path or _OPENAPI_PATH
    if not p.exists():
        raise ValueError(f"OpenAPI document not found: {p}")
    with open(p) as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise TypeError("OpenAPI document is not a mapping")
    version = doc.get("openapi", "")
    if not str(version).startswith("3.1"):
        raise ValueError(f"Expected OpenAPI 3.1, got {version!r}")
    return doc


def schema_components(doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the ``components.schemas`` section of the OpenAPI document."""
    d = doc or load_schema()
    return d.get("components", {}).get("schemas", {})
