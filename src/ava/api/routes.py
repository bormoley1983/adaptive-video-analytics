"""FastAPI route handlers for the AVA API.

Each route invokes one focused application use case and maps its result
to the public contract. No SQL, S3, broker, detector, or business state
transitions in routes (ADR-16).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..application.capabilities import CapabilityRegistry, get_capabilities
from ..application.health import ReadinessChecker
from ..application.identity import CallerIdentity
from ..application.version import BuildInfo
from .dto import (
    CapabilitiesResponse,
    CapabilityEntry,
    LivenessResponse,
    ReadinessDependency,
    ReadinessResponse,
    VersionResponse,
)

router = APIRouter()

def require_auth(request: Request) -> CallerIdentity:
    """Authenticate using the policy belonging to this application."""
    from .auth import create_auth_dependency, reject_authentication

    policy = request.app.state.identity_policy
    if policy is None:
        reject_authentication("authentication_unavailable", "Authentication is not configured")
    return create_auth_dependency(policy)(request)


@router.get("/health/live", response_model=LivenessResponse)
def health_live() -> LivenessResponse:
    """Process liveness — no dependency probes."""
    return LivenessResponse(status="alive")


@router.get("/health/ready", response_model=ReadinessResponse)
def health_ready(request: Request) -> ReadinessResponse:
    """API readiness — evaluates HealthContributor list.

    Empty contributor list means ready (no external dependencies in S2).
    """
    checker: ReadinessChecker = request.app.state.readiness_checker
    result = checker.check()
    deps = [
        ReadinessDependency(
            name=r.name,
            state=r.state.value,
            detail=r.detail,
            checked_at=r.checked_at,
        )
        for r in result.reports
    ]
    return ReadinessResponse(ready=result.ready, dependencies=deps)


@router.get("/version", response_model=VersionResponse)
def version(
    request: Request,
    caller: CallerIdentity = Depends(require_auth),  # noqa: B008
) -> VersionResponse:
    """Build/contract/runtime version manifest."""
    info: BuildInfo = request.app.state.build_info
    return VersionResponse(
        package_version=info.package_version,
        contract_version=info.contract_version,
        python_version=info.python_version,
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities(
    request: Request,
    caller: CallerIdentity = Depends(require_auth),  # noqa: B008
) -> CapabilitiesResponse:
    """Supported features and bounded limits.

    Starts empty in S2; later stories register capabilities.
    """
    registry: CapabilityRegistry = request.app.state.capability_registry
    caps = get_capabilities(registry)
    entries = [
        CapabilityEntry(
            id=c.id,
            name=c.name,
            available=c.available,
            limits=c.limits,
            detail=c.detail,
        )
        for c in caps
    ]
    return CapabilitiesResponse(capabilities=entries)
