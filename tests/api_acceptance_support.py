"""Disposable identity services shared by provider and HTTP acceptance tests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import docker
import httpx
import jwt
import yaml
from docker.errors import DockerException, NotFound

ROOT = Path(__file__).resolve().parents[1]
REALM = "acceptance"
AUDIENCE = "ava-api"
SITE = "site_71HFE865V215DE1CTEWH0AVNH9"
OTHER_SITE = "site_71HFE865V215DE1CTEWH0AVNH8"


def wait_until(probe, *, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {description}")
        time.sleep(min(0.2, remaining))


def _attribute_mapper(claim: str, *, multiple: bool = False) -> dict:
    return {
        "name": claim,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "config": {
            "user.attribute": "ava_" + claim,
            "claim.name": claim,
            "jsonType.label": "String",
            "access.token.claim": "true",
            "multivalued": str(multiple).lower(),
        },
    }


class IdentityStack:
    """Own exactly one network, identity database and Keycloak container."""

    def __init__(self, evidence_root: Path | None = None):
        self.name = "ava-identity-test-" + uuid.uuid4().hex[:12]
        self.evidence_root = evidence_root or Path(
            os.environ.get("AVA_TEST_IDENTITY_EVIDENCE_DIR", "results/identity-acceptance")
        )
        self.database_password = secrets.token_urlsafe(24)
        self.admin_password = secrets.token_urlsafe(24)
        self.user_password = secrets.token_urlsafe(24)
        self.service_secret = secrets.token_urlsafe(24)
        self._sensitive = [self.database_password, self.admin_password,
                           self.user_password, self.service_secret]
        self._images = {}
        self._docker = None
        self._network = None
        self._containers = []
        self.keycloak = None
        self._http = None
        self._admin_access_token = None
        self._service_id = None
        self.base_url = ""
        self.events = []

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/realms/{REALM}"

    @property
    def jwks_uri(self) -> str:
        return self.issuer + "/protocol/openid-connect/certs"

    def __enter__(self):
        try:
            self._start()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def _start(self):
        config = yaml.safe_load((ROOT / "ops/keycloak-compose.yml").read_text())
        images = {name: config["services"][name]["image"]
                  for name in ("postgres", "keycloak")}
        self._images = images
        self._docker = docker.from_env()
        self._docker.ping()
        for image in images.values():
            self._docker.images.get(image)
        labels = {"ava.acceptance.owner": self.name}
        self._network = self._docker.networks.create(self.name, labels=labels)
        database = self._docker.containers.create(
            images["postgres"], name=self.name + "-postgres", labels=labels,
            network=self._network.name, hostname="identity-postgres",
            environment={"POSTGRES_USER": "identity", "POSTGRES_DB": "identity",
                         "POSTGRES_PASSWORD": self.database_password},
            mem_limit="256m",
        )
        self._containers.append(database)
        database.start()
        wait_until(
            lambda: database.exec_run("pg_isready -U identity -d identity").exit_code == 0,
            timeout=30, description="disposable identity database",
        )
        self.keycloak = self._docker.containers.create(
            images["keycloak"], name=self.name + "-keycloak", labels=labels,
            network=self._network.name,
            command=["start-dev"],
            ports={"8080/tcp": ("127.0.0.1", None)},
            environment={
                "KC_BOOTSTRAP_ADMIN_USERNAME": "acceptance-admin",
                "KC_BOOTSTRAP_ADMIN_PASSWORD": self.admin_password,
                "KC_DB": "postgres",
                "KC_DB_URL": f"jdbc:postgresql://{database.name}:5432/identity",
                "KC_DB_USERNAME": "identity",
                "KC_DB_PASSWORD": self.database_password,
                "KC_HOSTNAME_STRICT": "false",
                "KC_HTTP_ENABLED": "true",
                "JAVA_OPTS_KC_HEAP": "-Xms128m -Xmx512m",
            },
            mem_limit="1g",
        )
        self._containers.append(self.keycloak)
        self.keycloak.start()
        self.keycloak.reload()
        port = self.keycloak.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]
        if port["HostIp"] != "127.0.0.1":
            raise RuntimeError("Identity test port must be loopback-only")
        self.base_url = "http://127.0.0.1:" + port["HostPort"]
        self._http = httpx.Client(timeout=3.0, trust_env=False)
        self.wait_ready("master")
        self._admin_access_token = self._grant("master", {
            "grant_type": "password", "client_id": "admin-cli",
            "username": "acceptance-admin", "password": self.admin_password,
        })
        self.admin("POST", "/admin/realms", json={
            "realm": REALM, "enabled": True, "accessTokenLifespan": 300,
            "registrationAllowed": False,
        })
        self._configure_identities()
        self.events.append("ready")

    def event_database(self) -> str:
        password = secrets.token_urlsafe(24)
        self._sensitive.append(password)
        database = self._docker.containers.create(
            self._images["postgres"], name=self.name + "-events",
            labels={"ava.acceptance.owner": self.name}, network=self._network.name,
            ports={"5432/tcp": ("127.0.0.1", None)}, mem_limit="256m",
            environment={"POSTGRES_USER": "events", "POSTGRES_DB": "events",
                         "POSTGRES_PASSWORD": password},
        )
        self._containers.append(database)
        database.start()
        wait_until(lambda: database.exec_run("pg_isready -h 127.0.0.1 -U events -d events").exit_code == 0,
                   timeout=30, description="disposable event database")
        database.reload()
        port = database.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]
        if port["HostIp"] != "127.0.0.1":
            raise RuntimeError("Event test port must be loopback-only")
        dsn = f"postgresql://events:{password}@127.0.0.1:{port['HostPort']}/events"
        self._sensitive.append(dsn)
        self.events.append("event-database-ready")
        return dsn

    def wait_ready(self, realm: str = REALM):
        def discovered():
            try:
                response = self._http.get(
                    f"{self.base_url}/realms/{realm}/.well-known/openid-configuration"
                )
                return response.status_code == 200
            except httpx.HTTPError:
                return False

        wait_until(discovered, timeout=90, description="disposable Keycloak discovery")

    def admin(self, method: str, path: str, **kwargs):
        response = self._http.request(
            method, self.base_url + path,
            headers={"Authorization": "Bearer " + self._admin_access_token}, **kwargs,
        )
        if response.is_error:
            raise RuntimeError(f"Disposable identity admin request failed: {response.status_code}")
        return response

    def _configure_identities(self):
        profile_path = f"/admin/realms/{REALM}/users/profile"
        profile = self.admin("GET", profile_path).json()
        for attribute, multiple in [("tenant", False), ("roles", True),
                                    ("allowed_sites", True)]:
            profile["attributes"].append({
                "name": "ava_" + attribute, "multivalued": multiple,
                "permissions": {"view": ["admin"], "edit": ["admin"]},
            })
        self.admin("PUT", profile_path, json=profile)
        mappers = [_attribute_mapper("tenant"), _attribute_mapper("roles", multiple=True),
                   _attribute_mapper("allowed_sites", multiple=True), {
                       "name": "api-audience", "protocol": "openid-connect",
                       "protocolMapper": "oidc-audience-mapper", "config": {
                           "included.custom.audience": AUDIENCE, "access.token.claim": "true",
                       },
                   }]
        for name, service in [("ava-human", False), ("ava-service", True)]:
            self.admin("POST", f"/admin/realms/{REALM}/clients", json={
                "clientId": name, "enabled": True, "protocol": "openid-connect",
                "publicClient": not service, "secret": self.service_secret,
                "directAccessGrantsEnabled": not service,
                "serviceAccountsEnabled": service, "standardFlowEnabled": False,
                "defaultClientScopes": ["basic"], "protocolMappers": mappers,
            })
        client = self.admin("GET", f"/admin/realms/{REALM}/clients",
                            params={"clientId": "ava-service"}).json()[0]
        self._service_id = client["id"]
        user = self.admin(
            "GET", f"/admin/realms/{REALM}/clients/{self._service_id}/service-account-user"
        ).json()
        self.admin("PUT", f"/admin/realms/{REALM}/users/{user['id']}", json={
            **user, "attributes": {"ava_tenant": ["tenant-a"], "ava_roles": ["admin"]},
        })
        for name, tenant, site in [("viewer", "tenant-a", SITE),
                                   ("other-tenant", "tenant-b", SITE),
                                   ("other-site", "tenant-a", OTHER_SITE)]:
            self.admin("POST", f"/admin/realms/{REALM}/users", json={
                "username": name, "enabled": True, "emailVerified": True,
                "firstName": "Acceptance", "lastName": "User",
                "email": name + "@example.invalid", "requiredActions": [],
                "attributes": {"ava_tenant": [tenant], "ava_roles": ["viewer"],
                               "ava_allowed_sites": [site]},
                "credentials": [{"type": "password", "value": self.user_password,
                                 "temporary": False}],
            })

    def _grant(self, realm: str, data: dict) -> str:
        response = self._http.post(
            f"{self.base_url}/realms/{realm}/protocol/openid-connect/token", data=data
        )
        if response.status_code != 200:
            raise RuntimeError(f"Disposable identity token grant failed: {response.status_code}")
        token = response.json()["access_token"]
        self._sensitive.append(token)
        if realm == REALM:
            claims = jwt.decode(token, options={"verify_signature": False})
            self.events.append("issued-claims:" + ",".join(sorted(claims)))
            if not all(key in claims for key in ("sub", "aud", "tenant", "roles")):
                raise RuntimeError("Disposable provider token is missing required fixture claims")
            audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
            if AUDIENCE not in audiences:
                raise RuntimeError("Disposable provider token is missing the API audience")
        return token

    def human_token(self, username: str = "viewer") -> str:
        return self._grant(REALM, {"grant_type": "password", "client_id": "ava-human",
                                   "username": username, "password": self.user_password})

    def service_token(self) -> str:
        return self._grant(REALM, {"grant_type": "client_credentials",
                                   "client_id": "ava-service",
                                   "client_secret": self.service_secret})

    def rotate_keys(self):
        realm = self.admin("GET", f"/admin/realms/{REALM}").json()
        self.admin("POST", f"/admin/realms/{REALM}/components", json={
            "name": "acceptance-rotation-" + uuid.uuid4().hex,
            "providerId": "rsa-generated", "providerType": "org.keycloak.keys.KeyProvider",
            "parentId": realm["id"],
            "config": {"priority": ["1000"], "enabled": ["true"], "active": ["true"],
                       "algorithm": ["RS256"], "keySize": ["2048"]},
        })
        self.events.append("keys-rotated")

    def short_lived_token(self) -> str:
        path = f"/admin/realms/{REALM}/clients/{self._service_id}"
        client = self.admin("GET", path).json()
        short = {**client, "attributes": {**client.get("attributes", {}),
                                          "access.token.lifespan": "1"}}
        try:
            self.admin("PUT", path, json=short)
            return self.service_token()
        finally:
            self.admin("PUT", path, json=client)

    def redact(self, text: str) -> str:
        for value in self._sensitive:
            text = text.replace(value, "[redacted]")
        return re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                      "[redacted-token]", text)

    def close(self):
        errors = []
        for container in reversed(self._containers):
            try:
                if container is self.keycloak:
                    evidence = self.evidence_root / self.name
                    evidence.mkdir(parents=True, exist_ok=True)
                    text = container.logs().decode(errors="replace")
                    (evidence / "keycloak.log").write_text(self.redact(text))
            except (DockerException, OSError):
                self.events.append("container-log-unavailable")
            try:
                container.remove(force=True, v=True)
            except NotFound:
                pass
            except DockerException as exc:
                errors.append(type(exc).__name__)
        if self._network is not None:
            try:
                self._network.remove()
            except NotFound:
                pass
            except DockerException as exc:
                errors.append(type(exc).__name__)
        if self._http is not None:
            self._http.close()
        if self._docker is not None:
            self._docker.close()
        self._containers = []
        self._network = None
        self.events.append("cleanup-failed" if errors else "cleaned")
        evidence = self.evidence_root / self.name
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "lifecycle.json").write_text(json.dumps({
            "owner": self.name, "events": self.events, "cleanup_errors": errors,
        }, indent=2) + "\n")
        if errors:
            raise RuntimeError("Disposable identity cleanup failed: " + ", ".join(errors))


APPROVED_EVENTS_SHA256 = "13eb3436cdb2ebc84344540dd785315961b6a873d847ff5d07121d32ca56780f"


def load_approved_events(path: Path) -> list[dict]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != APPROVED_EVENTS_SHA256:
        raise RuntimeError("Real-event fixture hash differs from the human-approved bytes")
    fixture = json.loads(raw)
    if fixture["record_count"] != len(fixture["records"]) or not fixture["records"]:
        raise RuntimeError("Approved event fixture must contain its declared records")
    return fixture["records"]


@contextmanager
def running_api(identity: IdentityStack, dsn: str):
    evidence = identity.evidence_root / (identity.name + "-http")
    evidence.mkdir(parents=True, exist_ok=True)
    proof_path = evidence / "server-package.json"
    log_path = evidence / "server.log"
    with socket.socket() as listener, log_path.open("w") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        process = subprocess.Popen(
            [sys.executable, "-I", str(ROOT / "tests/api_http_bootstrap.py"),
             str(listener.fileno())],
            stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True,
            pass_fds=(listener.fileno(),), cwd=ROOT,
        )
        started = False
        try:
            process.stdin.write(json.dumps({
                "dsn": dsn, "issuer": identity.issuer, "jwks_uri": identity.jwks_uri,
                "provenance_path": str(proof_path),
            }))
            process.stdin.close()
            with httpx.Client(base_url=base_url, timeout=3.0, trust_env=False) as client:
                def ready():
                    if process.poll() is not None:
                        raise RuntimeError("Installed API exited before readiness; see server.log")
                    try:
                        return client.get("/api/v1/health/ready").status_code == 200
                    except httpx.HTTPError:
                        return False

                wait_until(ready, timeout=30, description="installed HTTP API readiness")
                started = True
                yield client, proof_path
        finally:
            forced = False
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forced = True
                process.kill()
                process.wait(timeout=5)
            log.flush()
            log_path.write_text(identity.redact(log_path.read_text()))
            if started:
                proof = json.loads(proof_path.read_text())
                if forced or process.returncode != 0 or not proof.get("shutdown_complete"):
                    raise RuntimeError("Installed API did not complete its production lifespan")
