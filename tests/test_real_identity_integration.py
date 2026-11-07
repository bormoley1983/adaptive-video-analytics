"""Identity policy and access boundaries with an isolated real provider."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import httpx
import jwt
import pytest
from api_acceptance_support import SITE, IdentityStack, wait_until
from fastapi.testclient import TestClient

from ava.api.app import AppConfig, create_app
from ava.application.access import CameraGrant, camera_accessible
from ava.application.identity import TokenValidationError
from ava.infrastructure.jwks_verifier import JwksRsaVerifier

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def identity_stack():
    with IdentityStack() as stack:
        yield stack


@contextmanager
def verifier(stack, audience="ava-api"):
    with httpx.Client(timeout=1.0, trust_env=False) as client:
        yield JwksRsaVerifier(stack.issuer, stack.jwks_uri, audience, client=client)


def test_human_and_service_tokens_cross_protected_routes(identity_stack):
    tokens = [identity_stack.human_token(), identity_stack.service_token()]
    with verifier(identity_stack) as policy:
        callers = [policy.verify(token) for token in tokens]
        with TestClient(create_app(AppConfig(identity_policy=policy))) as client:
            statuses = [client.get("/api/v1/version", headers={
                "Authorization": "Bearer " + token, "Idempotency-Key": str(uuid.uuid4()),
            }).status_code for token in tokens]

    assert statuses == [200, 200]
    assert callers[0].subject != callers[1].subject
    assert callers[0].roles == frozenset({"viewer"})
    assert callers[0].site_ids == frozenset({SITE})
    assert callers[1].roles == frozenset({"admin"})
    assert callers[1].site_ids is None


@pytest.mark.parametrize("username,expected", [
    ("viewer", True), ("other-tenant", False), ("other-site", False),
])
def test_real_identity_claims_enforce_camera_access(identity_stack, username, expected):
    grant = CameraGrant("camera", SITE, frozenset({"tenant-a"}))
    token = identity_stack.human_token(username)
    with verifier(identity_stack) as policy:
        caller = policy.verify(token)

    assert camera_accessible(grant, caller) is expected


def test_tampered_real_token_is_rejected(identity_stack):
    token = identity_stack.service_token()
    parts = token.split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    with verifier(identity_stack) as policy, pytest.raises(TokenValidationError) as error:
        policy.verify(".".join(parts))

    assert error.value.code == "invalid"


def test_real_token_with_wrong_audience_is_rejected(identity_stack):
    token = identity_stack.service_token()
    with (verifier(identity_stack, audience="another-api") as policy,
          pytest.raises(TokenValidationError) as error):
        policy.verify(token)

    assert error.value.code == "invalid"


def test_rotated_signing_key_is_refreshed_without_losing_old_token(identity_stack):
    previous = identity_stack.service_token()
    with verifier(identity_stack) as policy:
        caller = policy.verify(previous)
        identity_stack.rotate_keys()
        rotated = identity_stack.service_token()
        current = policy.verify(rotated)

        assert jwt.get_unverified_header(previous)["kid"] != jwt.get_unverified_header(rotated)["kid"]
        assert current == caller
        assert policy.verify(previous) == caller


def test_expired_provider_token_is_rejected(identity_stack, monkeypatch):
    token = identity_stack.short_lived_token()
    with verifier(identity_stack) as policy:
        monkeypatch.setattr(policy, "_LEEWAY_SECONDS", 0)

        def expired():
            try:
                policy.verify(token)
                return False
            except TokenValidationError as exc:
                assert exc.code == "expired"
                return True

        assert wait_until(expired, timeout=5, description="issued token expiry")


def test_provider_outage_fails_closed_and_recovers(identity_stack, monkeypatch):
    token = identity_stack.service_token()
    with verifier(identity_stack) as policy:
        caller = policy.verify(token)
        # Require an actual JWKS refresh on each attempt without a minute-long test wait.
        monkeypatch.setattr(policy, "_REFRESH_INTERVAL", 0.0)
        monkeypatch.setattr(policy, "_RETRY_COOLDOWN", 0.0)
        try:
            identity_stack.keycloak.pause()
            with pytest.raises(TokenValidationError) as error:
                policy.verify(token)
            assert error.value.code == "identity_unavailable"
        finally:
            identity_stack.keycloak.unpause()
            identity_stack.wait_ready()

        assert policy.verify(token) == caller
