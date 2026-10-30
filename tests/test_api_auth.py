"""Unit tests for bearer-token authentication and authorization (F-1-S3).

Covers: JwksRsaVerifier positive/negative paths, route-level auth enforcement,
role matrix, health-endpoint openness, log redaction, and JWKS kid-miss refresh.
All tokens are generated in-memory with a fixed RSA key pair; no network calls.
"""

from __future__ import annotations

import base64
import time
from typing import Any
from unittest.mock import patch

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from ava.api.app import AppConfig, create_app
from ava.application.identity import (
    KNOWN_ROLES,
    TokenValidationError,
)
from ava.infrastructure.jwks_verifier import JwksRsaVerifier

# ── Fixed test key material ───────────────────────────────────────────────────

ISSUER = "https://idp.ava.test"
AUDIENCE = "ava-api"
KID = "test-key-1"


def _make_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return _make_rsa_key()


def _jwk_dict(private_key: rsa.RSAPrivateKey) -> dict[str, str]:
    """Serialize the public key to a JWK dict."""
    pub = private_key.public_key().public_numbers()

    def _b64u(n: int, length: int) -> str:
        raw = n.to_bytes(length, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "kid": KID,
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(pub.n, 256),
        "e": _b64u(pub.e, 3),
    }


@pytest.fixture(scope="module")
def jwk(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    return _jwk_dict(rsa_key)


@pytest.fixture()
def jwks_transport(jwk: dict[str, str]) -> httpx.MockTransport:
    """httpx transport serving the JWKS document."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def verifier(jwks_transport: httpx.MockTransport) -> JwksRsaVerifier:
    client = httpx.Client(transport=jwks_transport)
    return JwksRsaVerifier(issuer=ISSUER, jwks_uri="https://idp.ava.test/.well-known/jwks.json", audience=AUDIENCE, client=client)


def _make_token(
    rsa_key: rsa.RSAPrivateKey,
    *,
    sub: str = "user-1",
    tenant: str = "ten_01JTEST",
    roles: list[str] | None = None,
    allowed_sites: list[str] | None = None,
    issuer: str = ISSUER,
    exp_delta: float = 3600.0,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed RS256 test token."""
    now = time.time()
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": sub,
        "tenant": tenant,
        "roles": roles if roles is not None else ["viewer"],
        "exp": now + exp_delta,
        "iat": now,
    }
    if allowed_sites is not None:
        claims["allowed_sites"] = allowed_sites
    if extra_claims:
        claims.update(extra_claims)

    return pyjwt.encode(
        claims,
        rsa_key,
        algorithm="RS256",
        headers={"kid": KID},
    )


# ── Gate 1: JwksRsaVerifier positive path ────────────────────────────────────


class TestVerifierPositive:
    def test_valid_token_returns_identity(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key)
        identity = verifier.verify(token)
        assert identity.subject == "user-1"
        assert identity.tenant == "ten_01JTEST"
        assert identity.roles == frozenset({"viewer"})
        assert identity.site_ids is None

    def test_valid_token_with_sites(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, allowed_sites=["site_a", "site_b"])
        identity = verifier.verify(token)
        assert identity.site_ids == frozenset({"site_a", "site_b"})

    def test_valid_token_multiple_roles(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, roles=["viewer", "operator", "admin"])
        identity = verifier.verify(token)
        assert identity.roles == frozenset({"viewer", "operator", "admin"})

    @pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
    def test_each_known_role_accepted(self, verifier: JwksRsaVerifier, rsa_key, role):
        token = _make_token(rsa_key, roles=[role])
        identity = verifier.verify(token)
        assert role in identity.roles


# ── Gate 2: JwksRsaVerifier negative paths ───────────────────────────────────


class TestVerifierNegative:
    def test_malformed_token(self, verifier: JwksRsaVerifier):
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify("not.a.token")
        assert exc_info.value.code == "malformed"

    def test_expired_token(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, exp_delta=-10)
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "expired"

    def test_wrong_issuer(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, issuer="https://other-idp.test")
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "invalid"

    def test_missing_sub(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, sub="")
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code in ("missing_sub", "invalid")

    def test_missing_tenant(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, tenant="")
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code in ("missing_tenant", "invalid")

    def test_empty_roles(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, roles=[])
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code in ("missing_roles", "invalid")

    def test_unknown_role(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, roles=["superuser"])
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "unknown_role"

    def test_invalid_sites_type(self, verifier: JwksRsaVerifier, rsa_key):
        token = _make_token(rsa_key, extra_claims={"allowed_sites": "not-a-list"})
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "invalid_sites"


# ── Gate 5: JWKS kid-miss triggers re-fetch ──────────────────────────────────


class TestJwksKidMissRefresh:
    def test_refetch_on_unknown_kid(self, rsa_key, jwk):
        """First JWKS doc has key A; token uses key B → re-fetch picks it up."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First fetch: empty key set.
                return httpx.Response(200, json={"keys": []})
            # Second fetch (kid-miss): return the real key.
            return httpx.Response(200, json={"keys": [jwk]})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        verifier = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client
        )
        token = _make_token(rsa_key)
        identity = verifier.verify(token)
        assert identity.subject == "user-1"
        assert call_count == 2

    def test_unknown_kid_after_refetch_still_fails(self, rsa_key):
        """Kid not in JWKS even after re-fetch → unknown_key error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"keys": []})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        verifier = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client
        )
        token = _make_token(rsa_key)
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "unknown_key"

    def test_missed_kids_capped(self, rsa_key):
        """Beyond _MAX_MISSED_KIDS unique kids, no further fetches are triggered."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"keys": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)

        # Fill the missed-kid set to capacity.
        for i in range(JwksRsaVerifier._MAX_MISSED_KIDS):
            token = _make_token(rsa_key, extra_claims={"sub": f"user-{i}"})
            import jwt as _jwt
            claims = _jwt.decode(token, options={"verify_signature": False})
            token = _jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": f"attacker-{i}"})
            with pytest.raises(TokenValidationError):
                verifier.verify(token)

        calls_at_cap = calls

        # One more unique kid: should NOT trigger a fetch (cap reached).
        token_overflow = _make_token(rsa_key, extra_claims={"sub": "overflow"})
        import jwt as _jwt
        claims = _jwt.decode(token_overflow, options={"verify_signature": False})
        token_overflow = _jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "attacker-overflow"})
        with pytest.raises(TokenValidationError):
            verifier.verify(token_overflow)

        assert calls == calls_at_cap  # no additional fetch

    def test_failed_refresh_rejects_cached_key_until_recovery(self, rsa_key, jwk):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 2:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": [jwk]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)
        token = _make_token(rsa_key)
        verifier._cycle_start = time.monotonic() - verifier._REFRESH_INTERVAL - 1
        for _ in range(2):
            with pytest.raises(TokenValidationError) as exc_info:
                verifier.verify(token)
            assert exc_info.value.code == "identity_unavailable"
        assert calls == 2
        verifier._next_retry = 0.0
        assert verifier.verify(token).subject == "user-1"
        assert calls == 3

    def test_failed_refresh_bounded_retries(self, rsa_key, jwk):
        """Post-failure retries are bounded by _RETRY_COOLDOWN, not per-request."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json={"keys": [jwk]})
            return httpx.Response(503)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)
        token = _make_token(rsa_key)

        # Force periodic refresh and trigger a failure.
        verifier._cycle_start = time.monotonic() - (JwksRsaVerifier._REFRESH_INTERVAL + 1)
        with (
            patch.object(verifier, "_REFRESH_INTERVAL", 0),
            pytest.raises(TokenValidationError, match="Identity service unavailable"),
        ):
            verifier.verify(token)

        first_failure_calls = calls  # should be 2 (initial + one failed retry)

        # Many immediate requests: cooldown prevents further fetches.
        for _ in range(10):
            with (
                patch.object(verifier, "_REFRESH_INTERVAL", 0),
                pytest.raises(TokenValidationError, match="Identity service unavailable"),
            ):
                verifier.verify(token)

        assert calls == first_failure_calls  # no additional fetches during cooldown

        # After cooldown elapses, one more retry is allowed.
        verifier._next_retry = 0.0
        with (
            patch.object(verifier, "_REFRESH_INTERVAL", 0),
            pytest.raises(TokenValidationError, match="Identity service unavailable"),
        ):
            verifier.verify(token)
        assert calls == first_failure_calls + 1

    def test_missed_kid_retry_after_recovery(self, rsa_key, jwk):
        """A kid whose miss-fetch failed is retried on the next periodic
        refresh after the cooldown elapses, and is discovered when the IDP
        recovers."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json={"keys": []})
            if call_count == 2:
                # Miss-fetch fails (IDP down).
                return httpx.Response(503)
            # IDP recovers on the periodic refresh.
            return httpx.Response(200, json={"keys": [jwk]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)
        token = _make_token(rsa_key)

        # First miss: fetch fails (call 2), kid stays in _missed_kids.
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "identity_unavailable"
        assert call_count == 2
        assert KID in verifier._missed_kids

        # Simulate cooldown elapsing and periodic interval passing.
        verifier._next_retry = 0.0
        verifier._cycle_start = time.monotonic() - (JwksRsaVerifier._REFRESH_INTERVAL + 1)

        # Periodic refresh (backdated) triggers a fetch that succeeds.
        # The kid is in _missed_kids but the periodic path clears it when
        # the fetched document includes it.
        with patch.object(verifier, "_REFRESH_INTERVAL", 0):
            identity = verifier.verify(token)
        assert identity.subject == "user-1"
        assert call_count == 3

    def test_key_age_bound_during_outage(self, rsa_key, jwk):
        """If the IDP is unreachable for longer than _MAX_KEY_AGE, cached
        keys are rejected (fail closed) rather than validating indefinitely."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json={"keys": [jwk]})
            return httpx.Response(503)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)
        token = _make_token(rsa_key)

        # Backdate last_success beyond the key-age limit.
        verifier._last_success = time.monotonic() - (JwksRsaVerifier._MAX_KEY_AGE + 1)

        # Cached key is stale → identity_unavailable, not unknown_key.
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "identity_unavailable"

    def test_miss_fetch_budget(self, rsa_key):
        """Successful miss-fetches are bounded by _MAX_MISS_FETCHES per cycle.
        Beyond the budget, new kids are tracked in _missed_kids but do not
        trigger fetches until the periodic refresh resets the budget."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"keys": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)

        # Fill the miss-fetch budget: first 20 kids trigger fetches.
        for i in range(JwksRsaVerifier._MAX_MISS_FETCHES):
            token = _make_token(rsa_key, extra_claims={"sub": f"user-{i}"})
            import jwt as _jwt
            claims = _jwt.decode(token, options={"verify_signature": False})
            token = _jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": f"budget-{i}"})
            with pytest.raises(TokenValidationError):
                verifier.verify(token)

        calls_at_budget = calls  # initial + 20 miss-fetches
        assert calls_at_budget == 1 + JwksRsaVerifier._MAX_MISS_FETCHES

        # One more unique kid: budget exhausted → tracked but no fetch.
        token_overflow = _make_token(rsa_key, extra_claims={"sub": "overflow"})
        import jwt as _jwt
        claims = _jwt.decode(token_overflow, options={"verify_signature": False})
        token_overflow = _jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "budget-overflow"})
        with pytest.raises(TokenValidationError):
            verifier.verify(token_overflow)

        assert calls == calls_at_budget  # no additional fetch
        assert len(verifier._missed_kids) == JwksRsaVerifier._MAX_MISS_FETCHES + 1

    def test_cap_full_still_allows_periodic(self, rsa_key, jwk):
        """Even when _missed_kids is full of attacker IDs, a periodic refresh
        (triggered by the interval elapsing) still fetches and can discover
        a new legitimate signing key."""
        calls = 0
        cap_reached = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls, cap_reached
            calls += 1
            if not cap_reached:
                return httpx.Response(200, json={"keys": []})
            # Periodic refresh: IDP now serves the real key.
            return httpx.Response(200, json={"keys": [jwk]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)

        # Fill _missed_kids to capacity with attacker kids. The miss-fetch
        # budget (_MAX_MISS_FETCHES=20) limits fetches per cycle, so we
        # backdate _cycle_start every 20 kids to simulate time passing and
        # reset the budget for the next batch. Stop when the cap is full.
        import jwt as _jwt
        for i in range(JwksRsaVerifier._MAX_MISSED_KIDS):
            if i > 0 and i % JwksRsaVerifier._MAX_MISS_FETCHES == 0:
                verifier._cycle_start = time.monotonic() - (JwksRsaVerifier._REFRESH_INTERVAL + 1)
            token = _make_token(rsa_key, extra_claims={"sub": f"user-{i}"})
            claims = _jwt.decode(token, options={"verify_signature": False})
            token = _jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": f"attacker-{i}"})
            with pytest.raises(TokenValidationError):
                verifier.verify(token)
            if len(verifier._missed_kids) >= JwksRsaVerifier._MAX_MISSED_KIDS:
                cap_reached = True
                break

        assert len(verifier._missed_kids) == JwksRsaVerifier._MAX_MISSED_KIDS

        # Periodic refresh (backdate cycle_start): fetches despite full cap.
        verifier._cycle_start = time.monotonic() - (JwksRsaVerifier._REFRESH_INTERVAL + 1)
        token = _make_token(rsa_key)
        identity = verifier.verify(token)
        assert identity.subject == "user-1"

    def test_initial_fetch_failure_fails_closed(self, rsa_key):
        """If the initial JWKS fetch fails, no tokens can verify."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        # Constructor succeeds (fetch failure is caught internally).
        verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)
        token = _make_token(rsa_key)
        # First verify: no keys → identity_unavailable (fail closed).
        with pytest.raises(TokenValidationError) as exc_info:
            verifier.verify(token)
        assert exc_info.value.code == "identity_unavailable"


# ── Gate 3: Route-level auth — positive and negative ─────────────────────────


class TestRouteAuthPositive:
    @pytest.fixture()
    def auth_client(self, rsa_key, jwks_transport) -> tuple[TestClient, str]:
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER,
            jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http,
        )
        app = create_app(AppConfig(identity_policy=policy))
        token = _make_token(rsa_key)
        return TestClient(app), token

    @pytest.mark.parametrize("path", ["/api/v1/version", "/api/v1/capabilities"])
    def test_valid_token_returns_200(self, auth_client, path):
        client, token = auth_client
        r = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    @pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
    def test_all_roles_pass(self, rsa_key, jwks_transport, role):
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http
        )
        app = create_app(AppConfig(identity_policy=policy))
        client = TestClient(app)
        token = _make_token(rsa_key, roles=[role])
        r = client.get("/api/v1/version", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    @pytest.mark.parametrize("path", ["/api/v1/health/live", "/api/v1/health/ready"])
    def test_health_endpoints_open_without_token(self, auth_client, path):
        client, _ = auth_client
        r = client.get(path)
        assert r.status_code == 200


class TestRouteAuthNegative:
    @pytest.fixture()
    def auth_client(self, rsa_key, jwks_transport) -> tuple[TestClient, str]:
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER,
            jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http,
        )
        app = create_app(AppConfig(identity_policy=policy))
        token = _make_token(rsa_key)
        return TestClient(app), token

    @pytest.mark.parametrize("path", ["/api/v1/version", "/api/v1/capabilities"])
    def test_no_auth_header_returns_401(self, auth_client, path):
        client, _ = auth_client
        r = client.get(path)
        assert r.status_code == 401

    @pytest.mark.parametrize("path", ["/api/v1/version", "/api/v1/capabilities"])
    def test_malformed_token_returns_401(self, auth_client, path):
        client, _ = auth_client
        r = client.get(path, headers={"Authorization": "Bearer garbaggarbage"})
        assert r.status_code == 401

    def test_expired_token_returns_401(self, rsa_key, jwks_transport):
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http
        )
        app = create_app(AppConfig(identity_policy=policy))
        client = TestClient(app)
        token = _make_token(rsa_key, exp_delta=-10)
        r = client.get("/api/v1/version", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_wrong_issuer_returns_401(self, rsa_key, jwks_transport):
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http
        )
        app = create_app(AppConfig(identity_policy=policy))
        client = TestClient(app)
        token = _make_token(rsa_key, issuer="https://wrong-idp.test")
        r = client.get("/api/v1/version", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_unknown_role_returns_401(self, rsa_key, jwks_transport):
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http
        )
        app = create_app(AppConfig(identity_policy=policy))
        client = TestClient(app)
        token = _make_token(rsa_key, roles=["superuser"])
        r = client.get("/api/v1/version", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_cross_tenant_token_still_200(self, rsa_key, jwks_transport):
        """All tenants are accepted at the verifier level; site-scoping
        (404 for out-of-scope objects) applies to site-scoped resources
        in later stories. Cross-tenant enumeration is prevented there."""
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http
        )
        app = create_app(AppConfig(identity_policy=policy))
        client = TestClient(app)
        token = _make_token(rsa_key, tenant="ten_OTHER_TENANT")
        r = client.get("/api/v1/version", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


# ── Gate 4: Log redaction ────────────────────────────────────────────────────


class TestLogRedaction:
    def test_authorization_header_not_in_log(self, rsa_key, jwks_transport):
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http
        )
        app = create_app(AppConfig(identity_policy=policy))
        client = TestClient(app)
        token = _make_token(rsa_key)

        with patch("ava.api.middleware.logger") as mock_logger:
            r = client.get(
                "/api/v1/version",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200

        # The log call should not contain the token string.
        for call_args in mock_logger.info.call_args_list:
            log_line = " ".join(str(a) for a in call_args.args)
            assert token not in log_line, f"Token leaked into log: {log_line}"

    def test_log_contains_method_path_status(self, rsa_key, jwks_transport):
        client_http = httpx.Client(transport=jwks_transport)
        policy = JwksRsaVerifier(
            issuer=ISSUER, jwks_uri="https://idp.ava.test/jwks", audience=AUDIENCE, client=client_http
        )
        app = create_app(AppConfig(identity_policy=policy))
        client = TestClient(app)
        token = _make_token(rsa_key)

        with patch("ava.api.middleware.logger") as mock_logger:
            r = client.get(
                "/api/v1/version",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200

        call_args = mock_logger.info.call_args_list[0]
        log_line = " ".join(str(a) for a in call_args.args)
        assert "GET" in log_line
        assert "/api/v1/version" in log_line
        assert "200" in log_line


# ── Gate 6: OpenAPI document integrity ───────────────────────────────────────


class TestOpenApiDocument:
    def test_document_loads_with_paths(self):
        from ava.api.schema import load_schema

        doc = load_schema()
        assert "paths" in doc
        paths = doc["paths"]
        assert "/health/live" in paths
        assert "/health/ready" in paths
        assert "/version" in paths
        assert "/capabilities" in paths

    def test_security_scheme_present(self):
        from ava.api.schema import load_schema

        doc = load_schema()
        schemes = doc.get("components", {}).get("securitySchemes", {})
        assert "BearerAuth" in schemes
        assert schemes["BearerAuth"]["scheme"] == "bearer"

    def test_protected_routes_have_security(self):
        from ava.api.schema import load_schema

        doc = load_schema()
        for path in ("/version", "/capabilities"):
            op = doc["paths"][path]["get"]
            security = op.get("security")
            assert security is not None, f"{path} missing security requirement"
            assert any("BearerAuth" in s for s in security)

    def test_health_routes_have_no_security(self):
        from ava.api.schema import load_schema

        doc = load_schema()
        for path in ("/health/live", "/health/ready"):
            op = doc["paths"][path]["get"]
            security = op.get("security")
            assert security == [], f"{path} should have empty security list"


# ── Gate 7: Dependency direction (new files) ─────────────────────────────────

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
    "kafka",
    "prometheus_client",
}


def _get_imports(file_path) -> set[str]:
    import ast
    from pathlib import Path

    try:
        tree = ast.parse(Path(file_path).read_text())
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


class TestDependencyDirectionNewFiles:
    def test_application_identity_no_framework_imports(self):
        from pathlib import Path

        f = Path("src/ava/application/identity.py")
        imports = _get_imports(f)
        forbidden = imports & (_FORBIDDEN_MODULES | {"fastapi", "httpx", "jwt"})
        assert not forbidden, f"identity.py imports forbidden: {forbidden}"

    def test_infrastructure_verifier_only_allowed_imports(self):
        from pathlib import Path

        f = Path("src/ava/infrastructure/jwks_verifier.py")
        imports = _get_imports(f)
        forbidden = imports & (_FORBIDDEN_MODULES - {"httpx"})
        assert not forbidden, f"jwks_verifier.py imports forbidden: {forbidden}"


def test_empty_site_list_grants_no_sites(verifier, rsa_key):
    identity = verifier.verify(_make_token(rsa_key, allowed_sites=[]))
    assert identity.site_ids == frozenset()


def test_authentication_is_isolated_per_app():
    from ava.application.identity import CallerIdentity

    class Policy:
        def __init__(self, token):
            self.token = token

        def verify(self, token):
            if token != self.token:
                raise TokenValidationError("invalid", "invalid token")
            return CallerIdentity("subject", "tenant", frozenset({"viewer"}))

    first = TestClient(create_app(AppConfig(identity_policy=Policy("first"))))
    second = TestClient(create_app(AppConfig(identity_policy=Policy("second"))))
    assert first.get("/api/v1/version", headers={"Authorization": "Bearer first"}).status_code == 200
    assert second.get("/api/v1/version", headers={"Authorization": "Bearer first"}).status_code == 401
    assert TestClient(create_app()).get("/api/v1/version").status_code == 401


def test_authentication_failure_is_problem_detail():
    response = TestClient(create_app()).get("/api/v1/version")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert {"type", "title", "status", "code"} <= response.json().keys()


def test_served_contract_matches_packaged_contract():
    from ava.api.schema import load_schema

    assert TestClient(create_app()).get("/openapi.json").json() == load_schema()


def test_null_allowed_sites_rejected(verifier, rsa_key):
    """An explicit null allowed_sites is a malformed claim, not 'all sites'."""
    token = _make_token(rsa_key, extra_claims={"allowed_sites": None})
    with pytest.raises(TokenValidationError) as exc_info:
        verifier.verify(token)
    assert exc_info.value.code == "invalid_sites"


def test_absent_allowed_sites_grants_all(verifier, rsa_key):
    """Absent claim = all sites in tenant (documented contract)."""
    token = _make_token(rsa_key)  # no allowed_sites key
    identity = verifier.verify(token)
    assert identity.site_ids is None


def test_unknown_kids_are_rate_limited_per_kid(rsa_key):
    """Each unknown kid triggers at most one re-fetch per refresh cycle.
    A successful fetch that includes the kid removes it from _missed_kids."""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"keys": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    verifier = JwksRsaVerifier(ISSUER, "https://idp.ava.test/jwks", AUDIENCE, client)

    # Same kid: first verify triggers re-fetch (call 2), subsequent do not
    # (kid is in _missed_kids until a fetch that includes it clears it).
    token_a = _make_token(rsa_key)
    for _ in range(3):
        with pytest.raises(TokenValidationError):
            verifier.verify(token_a)
    assert calls == 2  # initial + one re-fetch

    # Different kid: triggers its own re-fetch (call 3).
    import jwt as _jwt
    claims = _jwt.decode(token_a, options={"verify_signature": False})
    token_b = _jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "rotated-key"})
    with pytest.raises(TokenValidationError):
        verifier.verify(token_b)
    assert calls == 3  # one re-fetch for the new kid
