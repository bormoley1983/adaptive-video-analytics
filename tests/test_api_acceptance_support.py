"""Owned-resource cleanup during partial identity-service setup."""

from unittest.mock import MagicMock, Mock

import pytest
from api_acceptance_support import IdentityStack
from docker.errors import APIError


@pytest.mark.parametrize("stage", ["network", "database", "keycloak"])
def test_partial_identity_startup_cleans_only_registered_resources(tmp_path, monkeypatch, stage):
    stack = IdentityStack(tmp_path)
    network = Mock()
    database = Mock()
    keycloak = Mock()
    keycloak.logs.return_value = b"safe startup log"
    docker = Mock()

    def start():
        stack._docker = docker
        stack._network = network
        if stage in {"database", "keycloak"}:
            stack._containers.append(database)
        if stage == "keycloak":
            stack.keycloak = keycloak
            stack._containers.append(keycloak)
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(stack, "_start", start)
    with pytest.raises(RuntimeError, match="injected setup failure"), stack:
        pytest.fail("Partial startup must not yield a service")

    network.remove.assert_called_once_with()
    docker.close.assert_called_once_with()
    if stage in {"database", "keycloak"}:
        database.remove.assert_called_once_with(force=True, v=True)
    else:
        database.remove.assert_not_called()
    if stage == "keycloak":
        keycloak.remove.assert_called_once_with(force=True, v=True)
    assert stack.events[-1] == "cleaned"


def test_identity_cleanup_continues_after_one_container_fails(tmp_path):
    stack = IdentityStack(tmp_path)
    first = Mock()
    second = Mock()
    second.remove.side_effect = APIError("injected removal failure")
    stack._containers = [first, second]
    network = stack._network = Mock()

    with pytest.raises(RuntimeError, match="cleanup failed"):
        stack.close()

    first.remove.assert_called_once_with(force=True, v=True)
    network.remove.assert_called_once_with()


def test_identity_evidence_redacts_credentials_and_tokens(tmp_path):
    stack = IdentityStack(tmp_path)
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature"

    result = stack.redact(f"{stack.admin_password} {stack.service_secret} {token}")

    assert stack.admin_password not in result
    assert stack.service_secret not in result
    assert token not in result


def test_changed_real_fixture_cannot_be_used(tmp_path):
    from api_acceptance_support import load_approved_events

    path = tmp_path / "changed.json"
    path.write_text('{"record_count": 1, "records": [{}]}')

    with pytest.raises(RuntimeError, match="human-approved bytes"):
        load_approved_events(path)


def test_missing_real_fixture_is_not_silently_skipped(tmp_path):
    from api_acceptance_support import load_approved_events

    with pytest.raises(FileNotFoundError):
        load_approved_events(tmp_path / "missing.json")


def test_approved_real_fixture_loads_without_altering_run_identity():
    from api_acceptance_support import ROOT, load_approved_events

    records = load_approved_events(ROOT / "tests/fixtures/f1_s6_real_events_sanitized.json")

    assert len(records) == 14
    assert {row["session_id"] for row in records if row["rule_type"] == "dwell"} == {"s-alias-01"}
    assert {row["session_id"] for row in records if row["rule_type"] == "line_crossing"} == {"s-alias-02"}


def test_event_database_setup_failure_removes_its_owned_container(tmp_path, monkeypatch):
    import api_acceptance_support as support

    stack = IdentityStack(tmp_path)
    container = Mock()
    network = Mock()
    docker = Mock()
    docker.containers.create.return_value = container
    stack._docker, stack._network = docker, network
    stack._images = {"postgres": "cached-image"}
    monkeypatch.setattr(support, "wait_until", Mock(side_effect=TimeoutError("database readiness")))
    try:
        with pytest.raises(TimeoutError, match="database readiness"):
            stack.event_database()
    finally:
        stack.close()

    container.remove.assert_called_once_with(force=True, v=True)
    network.remove.assert_called_once_with()


@pytest.mark.parametrize("hangs", [False, True])
def test_api_startup_failure_terminates_only_the_spawned_process(tmp_path, monkeypatch, hangs):
    from types import SimpleNamespace

    import api_acceptance_support as support

    process = Mock()
    process.poll.return_value = None
    if hangs:
        process.wait.side_effect = [support.subprocess.TimeoutExpired("owned-server", 10), 0]
    listener = MagicMock()
    listener.getsockname.return_value = ("127.0.0.1", 12345)
    socket_factory = MagicMock()
    socket_factory.return_value.__enter__.return_value = listener
    monkeypatch.setattr(support.socket, "socket", socket_factory)
    monkeypatch.setattr(support.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(support, "wait_until", Mock(side_effect=RuntimeError("API readiness")))
    identity = SimpleNamespace(evidence_root=tmp_path, name="owned-test", issuer="issuer",
                               jwks_uri="jwks", redact=lambda text: text)
    with (pytest.raises(RuntimeError, match="API readiness"),
          support.running_api(identity, "test-owned-dsn")):
        pytest.fail("A failed server must not yield a client")

    process.terminate.assert_called_once_with()
    if hangs:
        process.kill.assert_called_once_with()
        assert process.wait.call_count == 2
    else:
        process.wait.assert_called_once_with(timeout=10)
