"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _set_pg_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a default AVA_PG_DSN for all tests."""
    if not os.environ.get("AVA_PG_DSN"):
        monkeypatch.setenv(
            "AVA_PG_DSN",
            "postgresql://ava:***@127.0.0.1:5432/ava",
        )


@pytest.fixture(scope="session")
def pg_container(request):
    """PostgreSQL 18 container via Testcontainers (session-scoped)."""
    from testcontainers.community.postgres import PostgresContainer

    username = "ava"
    password = "testpass"
    dbname = "ava"

    container = PostgresContainer(
        image="postgres:18",
        username=username,
        password=password,
        dbname=dbname,
    )
    container.start()

    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    dsn = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"

    def _stop():
        container.stop()

    request.addfinalizer(_stop)

    return {
        "dsn": dsn,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "dbname": dbname,
    }
