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


@pytest.mark.parametrize("container_routing", [False, True])
def test_identity_issuer_uses_opt_in_internal_address(tmp_path, container_routing):
    stack = IdentityStack(tmp_path, container_routing=container_routing)
    stack.base_url = "http://127.0.0.1:49152"

    issuer = stack.issuer

    base = f"http://{stack.name}-keycloak:8080" if container_routing else stack.base_url
    assert issuer == base + "/realms/acceptance"
    assert stack.jwks_uri == issuer + "/protocol/openid-connect/certs"
    assert stack.base_url == "http://127.0.0.1:49152"


@pytest.mark.parametrize("container_routing", [False, True])
def test_identity_startup_configures_bounded_opt_in_network(tmp_path, monkeypatch, container_routing):
    import api_acceptance_support as support

    stack = IdentityStack(tmp_path, container_routing=container_routing)
    docker = Mock()
    network = docker.networks.create.return_value
    network.name = stack.name
    database, provider = Mock(), Mock()
    provider.attrs = {"NetworkSettings": {"Ports": {"8080/tcp": [
        {"HostIp": "127.0.0.1", "HostPort": "49152"},
    ]}}}
    docker.containers.create.side_effect = [database, provider]
    monkeypatch.setattr(support.docker, "from_env", lambda: docker)
    monkeypatch.setattr(support, "wait_until", Mock())
    monkeypatch.setattr(stack, "wait_ready", Mock())
    monkeypatch.setattr(stack, "_grant", Mock(return_value="temporary-admin-token"))
    monkeypatch.setattr(stack, "admin", Mock())
    monkeypatch.setattr(stack, "_configure_identities", Mock())
    try:
        stack._start()

        database_options = docker.containers.create.call_args_list[0].kwargs
        provider_options = docker.containers.create.call_args_list[1].kwargs
        assert provider_options["ports"] == {"8080/tcp": ("127.0.0.1", None)}
        assert stack.base_url == "http://127.0.0.1:49152"
        if container_routing:
            assert provider_options["environment"]["KC_HOSTNAME"] == (
                f"http://{stack.name}-keycloak:8080")
            assert provider_options["nano_cpus"] == 2_000_000_000
            assert provider_options["mem_limit"] == "2g"
            assert database_options["nano_cpus"] == 1_000_000_000
            assert database_options["mem_limit"] == "512m"
        else:
            assert "KC_HOSTNAME" not in provider_options["environment"]
            assert "nano_cpus" not in provider_options
            assert provider_options["mem_limit"] == "1g"
            assert database_options["mem_limit"] == "256m"
    finally:
        provider.logs.return_value = b"safe startup"
        stack.close()

    database.remove.assert_called_once_with(force=True, v=True)
    provider.remove.assert_called_once_with(force=True, v=True)


def test_api_container_startup_failure_removes_only_registered_resources(tmp_path, monkeypatch):
    from test_api_container_infra import _running_container

    stack = IdentityStack(tmp_path, container_routing=True)
    docker = stack._docker = Mock()
    network = stack._network = Mock()
    api = MagicMock()
    api.status = "created"
    api.logs.return_value = b"safe startup failure"
    api.start.side_effect = RuntimeError("injected container startup failure")
    docker.containers.create.return_value = api
    monkeypatch.setattr(stack, "_start", lambda: None)

    with (pytest.raises(RuntimeError, match="injected container startup failure"), stack,
          _running_container(stack, Mock(), tmp_path)):
        pytest.fail("Failed startup must not yield a client")

    assert docker.containers.create.call_args.kwargs["use_config_proxy"] is False
    api.remove.assert_called_once_with(force=True, v=True)
    api.stop.assert_not_called()
    network.remove.assert_called_once_with()
    docker.close.assert_called_once_with()
    assert stack.events[-1] == "cleaned"


def test_api_container_context_excludes_private_and_untracked_files(tmp_path, monkeypatch):
    import tarfile

    import test_api_container_infra as container_support

    paths = ["pyproject.toml", "README.md", "tests/Dockerfile.api-container",
             "tests/fixtures/packaged_api_requirements.txt", "tests/api_http_bootstrap.py",
             "tests/api_container_bootstrap.py", "src/ava/api/openapi.yaml", "src/ava/__init__.py",
             "src/ava/private.key", "docs/private.md", "results/private.json"]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic public input" if "private" not in name else "private input")
    monkeypatch.setattr(container_support, "ROOT", tmp_path)
    monkeypatch.setattr(container_support.subprocess, "check_output", lambda *a, **kw: (
        b"src/ava/api/openapi.yaml\0src/ava/__init__.py\0src/ava/private.key\0"))

    context, included = container_support._build_context()
    with context, tarfile.open(fileobj=context) as archive:
        members = archive.getnames()

    assert members == included
    assert "src/ava/api/openapi.yaml" in members
    assert "src/ava/private.key" not in members
    assert "docs/private.md" not in members
    assert "results/private.json" not in members
