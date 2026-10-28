"""Capability registry use case.

Reports supported features and bounded limits. Starts empty in S2;
later stories (S5, F-3) register event/evidence/search capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    """A single API capability entry."""

    id: str
    name: str
    available: bool = True
    limits: dict[str, str | int] = field(default_factory=dict)
    detail: str | None = None


class CapabilityRegistry:
    """Holds the set of registered capabilities.

    Starts empty; later stories register capabilities as they are implemented.
    """

    def __init__(self) -> None:
        self._capabilities: list[Capability] = []

    def register(self, capability: Capability) -> None:
        self._capabilities.append(capability)

    def list(self) -> list[Capability]:
        return list(self._capabilities)

    def get(self, capability_id: str) -> Capability | None:
        for c in self._capabilities:
            if c.id == capability_id:
                return c
        return None


def get_capabilities(registry: CapabilityRegistry) -> list[Capability]:
    """Return all registered capabilities."""
    return registry.list()
