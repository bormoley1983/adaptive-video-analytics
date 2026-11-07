"""Compose API use cases, configuration and infrastructure adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ..application.capabilities import CapabilityRegistry
from ..application.health import HealthContributor, ReadinessChecker
from ..application.identity import IdentityPolicy
from ..application.mutation import MutationAdmission, MutationTransactionFactory
from ..application.version import get_build_info
from .auth import AuthenticationError, problem_handler
from .idempotency_middleware import IdempotencyMiddleware
from .middleware import RequestLoggingMiddleware
from .routes import router
from .schema import load_schema

logger = logging.getLogger(__name__)


def compose_postgres_app(
    dsn: str,
    *,
    identity_policy: IdentityPolicy | None = None,
    package_version: str = "0.1.0",
    contract_version: str = "1.0.0",
    openapi_path: Path | None = None,
    mutation_pool_min: int = 1,
    mutation_pool_max: int = 4,
    mutation_admission_limit: int = 4,
) -> FastAPI:
    """Compose the full PostgreSQL-backed AVA API.

    Constructs all three repositories (events, grants, cameras) from a single
    DSN, validates the database schema, and wires them into the application.
    Also constructs the dedicated async mutation transaction adapter with its
    own connection pool (separate from the main query pools) for idempotent
    mutations with bounded admission.

    Schema validation: each repository's ``check_schema`` is called during
    composition. A database at an outdated or missing revision raises
    ``RuntimeError`` before the app is returned, so the service never starts
    against a schema it cannot serve.

    Readiness: a required database readiness contributor is registered so
    that ``/health/ready`` reports not-ready if the database becomes
    unavailable at runtime.

    Lifecycle: the app owns all connection pools (query + mutation). They are
    closed on application shutdown (via the FastAPI lifespan handler) and if
    any step after repository construction fails, already-created pools are
    cleaned up before the error propagates.

    Args:
        dsn: PostgreSQL connection string.
        identity_policy: Bearer-token identity policy (enables auth on protected routes).
        package_version: Reported by /version.
        contract_version: OpenAPI contract version.
        openapi_path: Optional path to a custom OpenAPI schema file.
        mutation_pool_min: Minimum connections in the mutation pool (default 1).
        mutation_pool_max: Maximum connections in the mutation pool (default 4).
            Must be >= mutation_admission_limit.
        mutation_admission_limit: Maximum concurrent mutations (default 4).
            Excess requests are rejected immediately with 409.

    Returns:
        Configured FastAPI instance with events, cameras and grants routes enabled.

    Raises:
        RuntimeError: When the database schema is not migrated to the expected
            revision (any of the three repositories fails its check).
        ValueError: When mutation_admission_limit > mutation_pool_max.
    """
    if mutation_admission_limit > mutation_pool_max:
        raise ValueError(
            f"mutation_admission_limit ({mutation_admission_limit}) must not "
            f"exceed mutation_pool_max ({mutation_pool_max})"
        )

    from ..persistence.cameras_postgres import PostgresCameraRepository
    from ..persistence.grants_postgres import PostgresGrantRepository
    from ..persistence.mutation_postgres import PostgresMutationTransaction
    from ..persistence.postgres import PostgresEventSink

    def _close_repos(*repos: object | None) -> None:
        for repo in repos:
            if repo is not None and hasattr(repo, "close"):
                try:
                    repo.close()  # type: ignore[union-attr]
                except Exception as close_err:  # noqa: BLE001
                    logger.warning(
                        "Failed to close %s during cleanup: %s",
                        type(repo).__name__,
                        close_err,
                    )

    event_repo = None
    grant_repo = None
    camera_repo = None
    mutation_txn = None
    try:
        event_repo = PostgresEventSink(dsn)
        grant_repo = PostgresGrantRepository(dsn)
        camera_repo = PostgresCameraRepository(dsn)

        # Schema validation: reject outdated or unmigrated databases before
        # the app is returned. Each adapter checks alembic_version against
        # its expected revision.
        event_repo.check_schema()
        grant_repo.check_schema()
        camera_repo.check_schema()

        # Mutation transaction adapter: dedicated async pool.
        # Opened and verified in the application lifespan.
        mutation_txn = PostgresMutationTransaction(
            dsn, pool_min=mutation_pool_min, pool_max=mutation_pool_max
        )

        # Build the app inside the cleanup boundary so that a failure during
        # app construction does not leak the already-open pools.
        app = create_app(
            AppConfig(
                package_version=package_version,
                contract_version=contract_version,
                openapi_path=openapi_path,
                identity_policy=identity_policy,
                grant_repository=grant_repo,
                event_repository=event_repo,
                camera_repository=camera_repo,
                mutation_txn=mutation_txn,
                mutation_admission=MutationAdmission(mutation_admission_limit),
            )
        )
    except Exception:
        _close_repos(event_repo, grant_repo, camera_repo)
        raise

    # Readiness contributor: the database is a required dependency.
    class _DbReadinessContributor:
        required = True
        name = "database"

        def check(self):
            from ..application.health import DependencyState, HealthReport

            try:
                event_repo.check_connection()
                return HealthReport(name=self.name, state=DependencyState.READY)
            except Exception as exc:  # noqa: BLE001
                return HealthReport(
                    name=self.name,
                    state=DependencyState.UNAVAILABLE,
                    detail=f"database check failed: {type(exc).__name__}",
                )

    app.state.readiness_checker.add_contributor(_DbReadinessContributor())

    # Owned lifecycle: open and close all pools on application startup/shutdown.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await mutation_txn.open()
            yield
        finally:
            try:
                await mutation_txn.close()
            finally:
                _close_repos(event_repo, grant_repo, camera_repo)


    app.router.lifespan_context = lifespan
    return app


@dataclass
class AppConfig:
    """Configuration for the AVA API application."""

    package_version: str = "0.1.0"
    contract_version: str = "1.0.0"
    openapi_path: Path | None = None
    health_contributors: list[HealthContributor] = field(default_factory=list)
    identity_policy: IdentityPolicy | None = None

    grant_repository: object | None = None
    event_repository: object | None = None
    camera_repository: object | None = None

    mutation_txn: MutationTransactionFactory | None = None
    mutation_admission: MutationAdmission | None = None


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create and configure the AVA FastAPI application.

    Args:
        config: Application configuration. Defaults to a minimal config
            with no health contributors. When
            ``identity_policy`` is set, bearer-token authentication is
            enabled on protected routes.

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

    app.add_exception_handler(AuthenticationError, problem_handler)
    app.add_exception_handler(HTTPException, problem_handler)


    app.state.readiness_checker = ReadinessChecker(cfg.health_contributors)
    app.state.build_info = get_build_info(
        package_version=cfg.package_version,
        contract_version=cfg.contract_version,
    )
    app.state.capability_registry = CapabilityRegistry()
    app.state.identity_policy = cfg.identity_policy

    # Request IDs and logging wrap identifier validation, including rejections.
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    # Serve the same contract that is distributed with the package.
    app.openapi = lambda: load_schema(cfg.openapi_path)


    app.include_router(router, prefix="/api/v1")

    # When grant and event repositories are configured, the camera
    # registry MUST also be configured.
    if (
        cfg.grant_repository is not None
        and cfg.event_repository is not None
        and cfg.camera_repository is None
    ):
        raise ValueError(
            "grant_repository and event_repository are configured but "
            "camera_repository is missing. The camera registry is required "
            "to maintain the invariant that every granted camera has a "
            "registry row (ADR-19). Use compose_postgres_app or supply a "
            "CameraRepository implementation."
        )

    # When a camera registry is configured, the grant repository MUST
    # support atomic grant + re-activation.
    if (
        cfg.camera_repository is not None
        and cfg.grant_repository is not None
        and not hasattr(cfg.grant_repository, "set_camera_grant_and_reactivate_camera")
    ):
        raise ValueError(
            "camera_repository is configured but grant_repository does not "
            "implement set_camera_grant_and_reactivate_camera. Atomic grant "
            "+ camera re-activation is required (ADR-19). Use "
            "compose_postgres_app or a GrantRepository that provides this "
            "method."
        )


    if cfg.grant_repository is not None and cfg.event_repository is not None:
        from .routes_events import router as events_router

        app.state.grant_repository = cfg.grant_repository
        app.state.event_repository = cfg.event_repository

        app.state.camera_repository = cfg.camera_repository

        app.state.mutation_txn = cfg.mutation_txn
        app.state.mutation_admission = cfg.mutation_admission or MutationAdmission(4)
        app.include_router(events_router, prefix="/api/v1")
        from ..application.capabilities import Capability

        app.state.capability_registry.register(
            Capability(
                id="events.read",
                name="Scoped event reads",
                limits={"max_page_size": 200, "default_page_size": 50},
                detail="Cursor-paginated, grant-scoped event and camera reads.",
            )
        )

    return app
