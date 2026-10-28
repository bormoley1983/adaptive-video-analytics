"""FastAPI application factory for the AVA API.

Composition root per ADR-16: validates configuration, constructs adapters,
supplies them to use cases. No business logic in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

from ..application.capabilities import CapabilityRegistry
from ..application.health import HealthContributor, ReadinessChecker
from ..application.version import get_build_info
from .routes import router


@dataclass
class AppConfig:
    """Configuration for the AVA API application."""

    package_version: str = "0.1.0"
    contract_version: str = "1.0.0"
    openapi_path: Path | None = None
    health_contributors: list[HealthContributor] = field(default_factory=list)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create and configure the AVA FastAPI application.

    Args:
        config: Application configuration. Defaults to a minimal config
            with no health contributors (S2 baseline).

    Returns:
        Configured FastAPI instance ready for serving or testing.
    """
    cfg = config or AppConfig()

    app = FastAPI(
        title="AVA Adaptive Video Analytics API",
        version=cfg.contract_version,
        docs_url="/docs",
        redoc_url=None,
    )

    # Composition: construct use cases and attach to app state.
    app.state.readiness_checker = ReadinessChecker(cfg.health_contributors)
    app.state.build_info = get_build_info(
        package_version=cfg.package_version,
        contract_version=cfg.contract_version,
    )
    app.state.capability_registry = CapabilityRegistry()

    # Register routes.
    app.include_router(router, prefix="/api/v1")

    return app
