"""Transport DTOs for the AVA API.

These are the wire-format models for the four S2 endpoints. They mirror
the OpenAPI YAML schemas; if they diverge, the YAML wins.
"""

from __future__ import annotations

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    """GET /health/live response."""

    status: str = "alive"


class ReadinessDependency(BaseModel):
    """A single dependency entry in the readiness response."""

    name: str
    state: str
    detail: str | None = None
    checked_at: str | None = None


class ReadinessResponse(BaseModel):
    """GET /health/ready response."""

    ready: bool
    dependencies: list[ReadinessDependency] = []


class VersionResponse(BaseModel):
    """GET /version response."""

    package_version: str
    contract_version: str
    python_version: str


class CapabilityEntry(BaseModel):
    """A single capability entry in the capabilities response."""

    id: str
    name: str
    available: bool = True
    limits: dict[str, str | int] = {}
    detail: str | None = None


class CapabilitiesResponse(BaseModel):
    """GET /capabilities response."""

    capabilities: list[CapabilityEntry] = []
