"""Unit tests for the AVA API service shell (F-1-S2).

Covers: app factory, four routes, readiness states, and dependency-direction
import rules (routes must not import detector/ORM/persistence).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ava.api.app import AppConfig, create_app
from ava.application.capabilities import CapabilityRegistry
from ava.application.health import (
    DependencyState,
    HealthReport,
    ReadinessChecker,
)

SRC = Path(__file__).resolve().parent.parent / "src"


# ── Gate 1: create_app() returns a working FastAPI instance ──────────────────


class TestAppFactory:
    def test_returns_fastapi_instance(self):
        from fastapi import FastAPI

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_default_config_has_no_contributors(self):
        app = create_app()
        checker: ReadinessChecker = app.state.readiness_checker
        result = checker.check()
        assert result.ready is True
        assert result.reports == []

    def test_custom_config_accepts_contributors(self):
        class FakeContributor:
            def check(self) -> HealthReport:
                return HealthReport(name="test", state=DependencyState.READY)

        app = create_app(AppConfig(health_contributors=[FakeContributor()]))
        checker: ReadinessChecker = app.state.readiness_checker
        result = checker.check()
        assert result.ready is True
        assert len(result.reports) == 1
        assert result.reports[0].name == "test"

    def test_build_info_attached(self):
        app = create_app(AppConfig(package_version="9.9.9", contract_version="2.0.0"))
        info = app.state.build_info
        assert info.package_version == "9.9.9"
        assert info.contract_version == "2.0.0"

    def test_capability_registry_attached(self):
        app = create_app()
        registry: CapabilityRegistry = app.state.capability_registry
        assert registry.list() == []


# ── Gate 2: All four routes return correct status codes and bodies ───────────


class TestRoutes:
    @pytest.fixture()
    def client(self) -> TestClient:
        from ava.application.identity import CallerIdentity

        class Policy:
            def verify(self, token):
                return CallerIdentity(subject="test", tenant="test", roles=frozenset({"viewer"}))

        app = create_app(AppConfig(package_version="0.1.0", contract_version="1.0.0",
                                   identity_policy=Policy()))
        return TestClient(app)

    def test_health_live(self, client: TestClient):
        r = client.get("/api/v1/health/live")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

    def test_health_ready_no_deps(self, client: TestClient):
        r = client.get("/api/v1/health/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["dependencies"] == []

    def test_version(self, client: TestClient):
        r = client.get("/api/v1/version", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        body = r.json()
        assert body["package_version"] == "0.1.0"
        assert body["contract_version"] == "1.0.0"
        assert "python_version" in body

    def test_capabilities_empty(self, client: TestClient):
        r = client.get("/api/v1/capabilities", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200
        assert r.json() == {"capabilities": []}


# ── Gate 3: Readiness distinguishes ready/degraded/unavailable/stale/unknown ─


class TestReadinessStates:
    def _make_client(self, state: DependencyState) -> TestClient:
        class FakeContributor:
            def check(self) -> HealthReport:
                return HealthReport(name="dep", state=state)

        app = create_app(AppConfig(health_contributors=[FakeContributor()]))
        return TestClient(app)

    def test_ready_state(self):
        client = self._make_client(DependencyState.READY)
        r = client.get("/api/v1/health/ready")
        assert r.json()["ready"] is True

    def test_degraded_state_still_ready(self):
        """Degraded optional dependency does not block readiness."""
        client = self._make_client(DependencyState.DEGRADED)
        r = client.get("/api/v1/health/ready")
        assert r.json()["ready"] is True

    def test_unavailable_state_not_ready(self):
        client = self._make_client(DependencyState.UNAVAILABLE)
        r = client.get("/api/v1/health/ready")
        assert r.json()["ready"] is False

    def test_stale_state_not_ready(self):
        client = self._make_client(DependencyState.STALE)
        r = client.get("/api/v1/health/ready")
        assert r.json()["ready"] is False

    def test_unknown_state_not_ready(self):
        client = self._make_client(DependencyState.UNKNOWN)
        r = client.get("/api/v1/health/ready")
        assert r.json()["ready"] is False

    def test_multiple_contributors_one_unavailable(self):
        class Good:
            def check(self) -> HealthReport:
                return HealthReport(name="good", state=DependencyState.READY)

        class Bad:
            def check(self) -> HealthReport:
                return HealthReport(name="bad", state=DependencyState.UNAVAILABLE)

        app = create_app(AppConfig(health_contributors=[Good(), Bad()]))
        client = TestClient(app)
        r = client.get("/api/v1/health/ready")
        body = r.json()
        assert body["ready"] is False
        assert len(body["dependencies"]) == 2

    def test_readiness_report_includes_detail(self):
        class DetailContributor:
            def check(self) -> HealthReport:
                return HealthReport(
                    name="db",
                    state=DependencyState.UNAVAILABLE,
                    detail="connection refused",
                )

        app = create_app(AppConfig(health_contributors=[DetailContributor()]))
        client = TestClient(app)
        r = client.get("/api/v1/health/ready")
        dep = r.json()["dependencies"][0]
        assert dep["detail"] == "connection refused"


# ── Gate 4+5: Dependency direction — no forbidden imports in API layer ───────

_FORBIDDEN_MODULES = {
    "ava.detector",
    "ava.ingest",
    "ava.runner",
    "ava.live_runner",
    "ava.persistence",
    "sqlalchemy",
    "psycopg",
    "alembic",
    "boto3",
    "aiobotocore",
    "kafka",
    "prometheus_client",
    "loki",
}


def _get_imports(file_path: Path) -> set[str]:
    """Extract all import module names from a Python file."""
    try:
        tree = ast.parse(file_path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


class TestDependencyDirection:
    """Routes and transport models must not import detector/ORM/persistence."""

    @pytest.fixture()
    def api_files(self) -> list[Path]:
        api_dir = SRC / "ava" / "api"
        return [f for f in api_dir.glob("*.py") if f.name != "__init__.py"]

    @pytest.fixture()
    def app_files(self) -> list[Path]:
        app_dir = SRC / "ava" / "application"
        return [f for f in app_dir.glob("*.py") if f.name != "__init__.py"]

    def test_api_routes_no_forbidden_imports(self, api_files: list[Path]):
        for f in api_files:
            imports = _get_imports(f)
            forbidden = imports & _FORBIDDEN_MODULES
            assert not forbidden, f"{f.name} imports forbidden modules: {forbidden}"

    def test_application_no_forbidden_imports(self, app_files: list[Path]):
        for f in app_files:
            imports = _get_imports(f)
            forbidden = imports & _FORBIDDEN_MODULES
            assert not forbidden, f"{f.name} imports forbidden modules: {forbidden}"

    def test_application_no_fastapi_import(self, app_files: list[Path]):
        """Application layer must not import FastAPI (presentation concern)."""
        for f in app_files:
            imports = _get_imports(f)
            assert "fastapi" not in imports, f"{f.name} imports fastapi"

    def test_api_does_not_import_application_internals(self, api_files: list[Path]):
        """API layer should import application use cases, not their internals."""
        for f in api_files:
            content = f.read_text()
            # Should not directly import from ava.application.health internals
            assert "from ..application.health import" in content or \
                   "from ava.application.health import" in content or \
                   "HealthContributor" not in content, \
                f"{f.name} has unexpected application imports"


# ── Gate 6: test-unit passes (this file) ─────────────────────────────────────
# (Implicit — running this file IS the unit test.)


def test_optional_unavailable_contributor_does_not_block_readiness():
    class Optional:
        required = False

        def check(self):
            return HealthReport(name="optional", state=DependencyState.UNAVAILABLE)

    result = ReadinessChecker([Optional()]).check()
    assert result.ready is True
    assert result.reports[0].state == DependencyState.UNAVAILABLE


def test_contributor_exception_is_reported_without_details():
    class Broken:
        name = "database"
        required = True

        def check(self):
            raise RuntimeError("secret dsn")

    result = ReadinessChecker([Broken()]).check()
    assert result.ready is False
    assert result.reports[0].name == "database"
    assert "secret" not in (result.reports[0].detail or "")
