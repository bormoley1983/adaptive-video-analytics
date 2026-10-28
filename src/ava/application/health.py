"""Health and readiness use cases.

Implements the HealthContributor port and ReadinessChecker use case per
ADR-15 required-versus-optional dependency rules. Prometheus/Loki availability
is NOT a runtime prerequisite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol


class DependencyState(str, Enum):
    """Typed dependency state for health reporting."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HealthReport:
    """Result of a single health contributor check."""

    name: str
    state: DependencyState
    detail: str | None = None
    checked_at: str | None = None

    def __post_init__(self) -> None:
        if self.checked_at is None:
            object.__setattr__(
                self, "checked_at", datetime.now(UTC).isoformat()
            )


class HealthContributor(Protocol):
    """Outbound port: report one dependency's state and freshness."""

    def check(self) -> HealthReport: ...


@dataclass(frozen=True)
class ReadinessResult:
    """Aggregated readiness state from all contributors."""

    ready: bool
    reports: list[HealthReport] = field(default_factory=list)

    @property
    def states(self) -> dict[str, DependencyState]:
        return {r.name: r.state for r in self.reports}


class ReadinessChecker:
    """Aggregates HealthContributor reports into a readiness verdict.

    Empty contributor list means ready (no external dependencies).
    Any required contributor in UNAVAILABLE state makes the service not ready.
    Optional contributor failures degrade but do not block readiness.
    """

    def __init__(self, contributors: list[HealthContributor] | None = None) -> None:
        self._contributors = contributors or []

    def check(self) -> ReadinessResult:
        reports = [c.check() for c in self._contributors]
        ready = all(r.state in (DependencyState.READY, DependencyState.DEGRADED) for r in reports)
        return ReadinessResult(ready=ready, reports=reports)

    def add_contributor(self, contributor: HealthContributor) -> None:
        self._contributors.append(contributor)
