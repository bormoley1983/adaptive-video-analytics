"""Installed API authentication across an owned container network."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import docker
import httpx
import jwt
import pytest
from api_acceptance_support import ROOT, IdentityStack, wait_until
from docker.errors import NotFound

pytestmark = pytest.mark.infra
PYTHON_IMAGE = (
    "python:3.12-slim@sha256:"
    "a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
)


def _build_context():
    sources = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "src"], cwd=ROOT,
    ).decode().split("\0")
    paths = ["pyproject.toml", "README.md", "tests/Dockerfile.api-container",
             "tests/fixtures/packaged_api_requirements.txt", "tests/api_http_bootstrap.py",
             "tests/api_container_bootstrap.py"]
    paths.extend(path for path in sources if path.endswith((".py", ".yaml")))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as context:
        for name in sorted(paths):
            path = ROOT / name
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise RuntimeError("Image context must contain only owned public files")
            context.add(path, arcname=name, recursive=False)
    archive.seek(0)
    return archive, sorted(paths)


def _capacity():
    available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))) * 1024
    cores = os.cpu_count() or 0
    load = os.getloadavg()[0]
    snapshot = {"cpus": cores, "load_one_minute": load, "available_memory_bytes": available}
    if cores - load < 4 or available < 4 * 1024**3:
        raise RuntimeError("Insufficient current CPU/RAM capacity for the bounded routing proof")
    return snapshot


def _read_proof(container):
    chunks, _ = container.get_archive("/tmp/api-provenance.json")
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
        return json.load(archive.extractfile("api-provenance.json"))


@contextmanager
def _running_container(identity, image, evidence):
    container = identity._docker.containers.create(
        image.id, name=identity.name + "-api", network=identity._network.name,
        labels={"ava.acceptance.owner": identity.name},
        ports={"8080/tcp": ("127.0.0.1", None)},
        stdin_open=True, mem_limit="512m", nano_cpus=1_000_000_000, use_config_proxy=False,
    )
    identity._containers.append(container)
    started = False
    try:
        with container.attach_socket(params={"stdin": 1, "stream": 1, "logs": 0}) as stream:
            container.start()
            stream._sock.sendall((json.dumps({
                "issuer": identity.issuer, "jwks_uri": identity.jwks_uri,
            }) + "\n").encode())
        container.reload()
        port = container.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]
        assert port["HostIp"] == "127.0.0.1"
        with httpx.Client(base_url="http://127.0.0.1:" + port["HostPort"],
                          timeout=3, trust_env=False) as client:
            def ready():
                container.reload()
                if container.status != "running":
                    raise RuntimeError("API container exited before readiness; see api.log")
                try:
                    return client.get("/api/v1/health/ready").status_code == 200
                except httpx.HTTPError:
                    return False

            wait_until(ready, timeout=30, description="installed API container readiness")
            started = True
            yield client, container
    finally:
        container.reload()
        if container.status == "running":
            container.stop(timeout=10)
        (evidence / "api.log").write_text(identity.redact(container.logs().decode(errors="replace")))
        if started:
            proof = _read_proof(container)
            (evidence / "api-provenance.json").write_text(json.dumps(proof, indent=2) + "\n")
            container.reload()
            assert container.attrs["State"]["ExitCode"] == 0
            assert proof["startup_complete"] and proof["shutdown_complete"]


def _assert_removed(client, containers, network):
    for identifier in containers:
        with pytest.raises(NotFound):
            client.containers.get(identifier)
    with pytest.raises(NotFound):
        client.networks.get(network)


def test_installed_api_authenticates_over_owned_provider_network():
    evidence = Path(os.environ.get("AVA_TEST_IDENTITY_EVIDENCE_DIR",
                                   "results/api-container-acceptance")) / uuid.uuid4().hex
    evidence.mkdir(parents=True)
    capacity = _capacity()
    (evidence / "capacity.json").write_text(json.dumps(capacity, indent=2) + "\n")
    context, files = _build_context()
    (evidence / "build-context.json").write_text(json.dumps(files, indent=2) + "\n")
    image = None
    containers, network = [], None
    with closing(docker.from_env()) as client:
        client.ping()
        base = client.images.get(PYTHON_IMAGE)
        identity = IdentityStack(evidence, container_routing=True)
        tag = identity.name + ":api"
        try:
            with context, (evidence / "image-build.log").open("w") as log:
                for event in client.api.build(
                    fileobj=context, custom_context=True, dockerfile="tests/Dockerfile.api-container",
                    tag=tag, pull=False, rm=True, forcerm=True, decode=True,
                    container_limits={"memory": 512 * 1024**2, "cpushares": 1024},
                ):
                    log.write(identity.redact(json.dumps(event)) + "\n")
                    log.flush()
                    if "error" in event:
                        raise RuntimeError("API image build failed; see image-build.log")
            image = client.images.get(tag)
            with identity:
                network = identity._network.id
                with _running_container(identity, image, evidence) as (http, api):
                    containers = [container.id for container in identity._containers]
                    identity.keycloak.reload()
                    provider_address = identity.keycloak.attrs["NetworkSettings"]["Networks"][
                        identity._network.name]["IPAddress"]
                    proof = _read_proof(api)
                    assert proof["noneditable"] and proof["startup_complete"]
                    assert proof["issuer"] == proof["discovery_issuer"] == identity.issuer
                    assert proof["jwks_uri"] == identity.jwks_uri
                    assert proof["provider_addresses"] == [provider_address]
                    assert set(proof["module_sha256"]) == set(proof["modules"])
                    outcomes = {}
                    for route in ("live", "ready"):
                        response = http.get("/api/v1/health/" + route)
                        outcomes["health_" + route] = response.status_code
                        assert response.status_code == 200
                    headers = {"Idempotency-Key": str(uuid.uuid4())}
                    response = http.get("/api/v1/version", headers=headers)
                    outcomes["missing_authentication"] = response.status_code
                    assert response.status_code == 401
                    for kind, token in (("human", identity.human_token()),
                                        ("service", identity.service_token())):
                        claims = jwt.decode(token, options={"verify_signature": False})
                        assert claims["iss"] == identity.issuer
                        response = http.get("/api/v1/version", headers={
                            **headers, "Authorization": "Bearer " + token,
                        })
                        outcomes[kind] = {"status": response.status_code, "issuer": claims["iss"]}
                        assert response.status_code == 200
                        assert response.json()["package_version"] == proof["version"]
                    (evidence / "routing.json").write_text(json.dumps({
                        "base_commit": subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                        ).strip(),
                        "base_image": {"reference": PYTHON_IMAGE, "id": base.id},
                        "api_image": image.id, "identity_images": identity._images,
                        "network": network, "network_name": identity._network.name,
                        "containers": containers, "provider_address": provider_address,
                        "outcomes": outcomes,
                    }, indent=2) + "\n")
        finally:
            try:
                # IdentityStack also owns API cleanup when launch fails before readiness.
                if identity._containers or identity._network is not None:
                    containers = [container.id for container in identity._containers]
                    network = identity._network.id if identity._network else network
                    identity.close()
                if network is not None:
                    _assert_removed(client, containers, network)
                    (evidence / "cleanup.json").write_text(json.dumps({
                        "containers_absent": containers, "network_absent": network,
                    }, indent=2) + "\n")
            finally:
                if image is not None:
                    client.images.remove(tag)
