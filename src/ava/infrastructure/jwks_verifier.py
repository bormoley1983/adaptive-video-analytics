"""OIDC/JWT access-token verifier (IdentityPolicy adapter).

Verifies RS256 bearer tokens against the deployment identity provider's
JWKS document using Authlib's maintained JOSE implementation. Caches the
key set in memory with a bounded freshness window; re-fetches on unknown
key ids and periodically. Once a refresh is required, its failure rejects
protected requests (fail closed).

Verified library behavior (authlib 1.6.12, Python 3.11):
- ``JWTClaims.validate_iss()`` enforces the configured issuer when the
  ``iss`` claim option carries a value; a mismatch raises
  ``InvalidClaimError``.
- ``JWTClaims.validate_aud()`` enforces the configured audience when the
  ``aud`` option carries a value or values list; a missing or mismatched
  audience raises ``InvalidClaimError``.
- ``JWTClaims.validate_exp(now, leeway)`` rejects expired tokens and
  non-numeric expiry values. It is NOT called automatically by
  ``JsonWebToken.decode`` — the caller must invoke it explicitly.
- Signature verification is performed by ``JsonWebToken.decode`` with the
  key resolved from the JWKS cache by ``kid``; an unknown kid raises before
  any signature check.
"""

from __future__ import annotations

import base64
import json as _json
import threading
import time
from typing import Any

import httpx
from authlib.jose import (
    JoseError,
    JsonWebKey,
    JsonWebToken,
    KeySet,
)
from authlib.jose.errors import (
    BadSignatureError,
    DecodeError,
    ExpiredTokenError,
    InvalidClaimError,
)

from ava.application.identity import (
    KNOWN_ROLES,
    CallerIdentity,
    TokenValidationError,
)


class JwksRsaVerifier:
    """Concrete IdentityPolicy adapter using Authlib + httpx.

    Args:
        issuer: Expected ``iss`` claim value.
        jwks_uri: URL of the JWKS document (JSON).
        audience: Expected API ``aud`` claim value (string or list).
        client: Optional httpx.Client for dependency injection in tests.
            If omitted, a default client is created and owned by this instance.
    """

    _REFRESH_INTERVAL = 60.0   # seconds between periodic refreshes
    _MAX_MISSED_KIDS = 100     # bound on tracked unknown kids (DoS protection)
    _RETRY_COOLDOWN = 5.0      # min seconds between fetch attempts after failure
    _MAX_MISS_FETCHES = 20     # successful miss-fetches per refresh cycle
    _MAX_KEY_AGE = 3600.0      # max seconds a cached key set may be used (1h)
    _LEEWAY_SECONDS = 10       # clock-skew leeway for expiry validation

    def __init__(
        self,
        issuer: str,
        jwks_uri: str,
        audience: str | list[str],
        client: httpx.Client | None = None,
    ) -> None:
        self._issuer = issuer
        self._jwks_uri = jwks_uri
        self._audience = (
            [audience] if isinstance(audience, str) else list(audience)
        )
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None
        self._keys: KeySet | None = None
        self._lock = threading.Lock()
        self._last_success = 0.0   # last successful fetch (drives key-age check)
        self._cycle_start = 0.0    # start of current refresh cycle (drives periodic)
        self._next_retry = 0.0     # earliest time a failed-fetch retry is allowed
        self._refresh_failed = False
        self._ever_succeeded = False  # True after first successful JWKS fetch
        self._missed_kids: set[str] = set()
        self._miss_fetch_count = 0   # successful miss-fetches since last cycle
        self._fetch()

    def _fetch(self) -> bool:
        """Fetch the JWKS document and build the key cache.

        Returns True on success, False on failure.

        On success: updates _last_success, replaces the key cache, removes
        resolved kids from _missed_kids (only those now present). Kids still
        absent remain tracked so they don't trigger repeated fetches. The
        miss-fetch counter is NOT reset here — it tracks cumulative successful
        miss-fetches per refresh cycle and is only reset by a periodic fetch.
        On failure: set the retry cooldown and reject tokens until a
        successful refresh restores trust in the cached keys.
        """
        try:
            resp = self._client.get(self._jwks_uri)
            resp.raise_for_status()
            doc = resp.json()
            raw_keys = [k for k in doc.get("keys", []) if isinstance(k, dict)]
            keys = []
            for raw in raw_keys:
                try:
                    keys.append(JsonWebKey.import_key(raw))
                except (JoseError, ValueError, TypeError):
                    # Skip malformed JWK entries.
                    pass
            keyset = KeySet(keys=keys)
            present_kids = {
                k.kid for k in keys if isinstance(getattr(k, "kid", None), str)
            }
            with self._lock:
                self._keys = keyset
                self._last_success = time.monotonic()
                self._cycle_start = time.monotonic()
                self._ever_succeeded = True
                self._refresh_failed = False
                self._missed_kids -= present_kids
            return True
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            with self._lock:
                self._next_retry = time.monotonic() + self._RETRY_COOLDOWN
                self._refresh_failed = True
            return False

    def _get_key(self, kid: str) -> Any:
        """Return the key for *kid*, re-fetching when needed.

        Fetch triggers (all bounded by a unified cooldown):
        - Periodic: _REFRESH_INTERVAL since last successful fetch.
        - Miss: kid not in cache AND not yet tried this cycle AND the
          miss-fetch budget (_MAX_MISS_FETCHES) has remaining slots. Each
          unique kid gets at most one re-fetch opportunity per refresh
          cycle. The missed-kid set is capped at _MAX_MISSED_KIDS; beyond
          that, new kids wait for the periodic refresh (no unbounded remote
          requests from attacker-controlled kid values).

        A fetch attempt is only made when now >= _next_retry, bounding
        post-failure retries to one per _RETRY_COOLDOWN regardless of
        request volume. If no keys are cached at all (initial fetch failed
        or IDP never recovered), raises identity_unavailable immediately —
        fail closed.

        Key age: if the cache is older than _MAX_KEY_AGE and the IDP is
        unreachable, tokens signed by cached keys are rejected rather than
        validated indefinitely during a prolonged outage.
        """
        # Phase 1: decide whether a fetch is needed (read state under lock).
        with self._lock:
            now = time.monotonic()
            is_periodic = now - self._cycle_start >= self._REFRESH_INTERVAL
            kid_known = self._keys is not None and _find_kid(self._keys, kid) is not None
            is_miss = (
                not kid_known
                and kid not in self._missed_kids
                and len(self._missed_kids) < self._MAX_MISSED_KIDS
            )
            should_fetch = (is_periodic or is_miss) and now >= self._next_retry
            if should_fetch and is_miss:
                self._missed_kids.add(kid)

        # Phase 2: perform fetch outside the lock to avoid deadlock.
        if should_fetch:
            with self._lock:
                budget_ok = (
                    True if not is_miss
                    else self._miss_fetch_count < self._MAX_MISS_FETCHES
                )
            if budget_ok:
                fetch_ok = self._fetch()
                if fetch_ok:
                    with self._lock:
                        if is_miss:
                            self._miss_fetch_count += 1
                        else:
                            # Periodic fetch: reset the miss-fetch budget.
                            self._miss_fetch_count = 0

        # Phase 3: read state after potential fetch.
        with self._lock:
            now = time.monotonic()
            if not self._ever_succeeded or self._refresh_failed:
                raise TokenValidationError(
                    "identity_unavailable", "Identity service unavailable"
                )
            if now - self._last_success > self._MAX_KEY_AGE:
                raise TokenValidationError(
                    "identity_unavailable",
                    "Key cache expired; identity service unreachable",
                )
            key = _find_kid(self._keys, kid)
            if key is None:
                raise TokenValidationError("unknown_key", "token signed by unknown key")
            return key

    def verify(self, token: str) -> CallerIdentity:
        """Verify *token* and return the caller's identity.

        Raises:
            TokenValidationError: on any verification failure.
        """
        # Extract kid from the unverified JWT header (base64url-encoded JSON).
        try:
            segments = token.split(".")
            if len(segments) != 3:
                raise ValueError("not a JWS compact serialization")
            header_b64 = segments[0] + "=" * (-len(segments[0]) % 4)
            header = _json.loads(base64.urlsafe_b64decode(header_b64))
        except (ValueError, TypeError, _json.JSONDecodeError) as exc:
            raise TokenValidationError("malformed", str(exc)) from exc

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenValidationError("missing_kid", "token has no key id")

        key = self._get_key(kid)

        claims_options: dict[str, Any] = {
            "iss": {"value": self._issuer},
            "aud": {"values": self._audience},
        }
        try:
            jwt_obj = JsonWebToken(algorithms=["RS256"])
            claims = jwt_obj.decode(
                token,
                key=key,
                claims_options=claims_options,
            )
        except ExpiredTokenError as exc:
            raise TokenValidationError("expired", "token has expired") from exc
        except BadSignatureError as exc:
            raise TokenValidationError("invalid", "signature verification failed") from exc
        except InvalidClaimError as exc:
            raise TokenValidationError("invalid", f"claim validation failed: {exc}") from exc
        except (DecodeError, JoseError) as exc:
            raise TokenValidationError("malformed", str(exc)) from exc

        try:
            claims.validate_iss()
            claims.validate_aud()
            claims.validate_exp(time.time(), self._LEEWAY_SECONDS)
        except ExpiredTokenError as exc:
            raise TokenValidationError("expired", "token has expired") from exc
        except InvalidClaimError as exc:
            raise TokenValidationError("invalid", f"claim validation failed: {exc}") from exc

        return _claims_to_identity(claims)


def _find_kid(keyset: KeySet | None, kid: str) -> Any:
    """Return the key for *kid* from a JWKS cache, or None."""
    if keyset is None:
        return None
    try:
        return keyset.find_by_kid(kid)
    except (ValueError, KeyError):
        return None


def _claims_to_identity(claims: Any) -> CallerIdentity:
    """Map validated claims to a CallerIdentity.

    Raises:
        TokenValidationError: if required claims are missing or roles
            contain unknown values.
    """
    sub = claims.get("sub")
    tenant = claims.get("tenant")
    roles_raw = claims.get("roles")

    if not isinstance(sub, str) or not sub:
        raise TokenValidationError("missing_sub", "subject claim is empty")
    if not isinstance(tenant, str) or not tenant:
        raise TokenValidationError("missing_tenant", "tenant claim is empty")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise TokenValidationError("missing_roles", "roles claim is empty")

    if not all(isinstance(role, str) for role in roles_raw):
        raise TokenValidationError("invalid_roles", "roles must be strings")
    roles = frozenset(roles_raw)
    unknown = roles - KNOWN_ROLES
    if unknown:
        raise TokenValidationError(
            "unknown_role", "token contains an unrecognised role"
        )

    if "allowed_sites" in claims:
        site_ids_raw = claims["allowed_sites"]
        if not isinstance(site_ids_raw, list):
            raise TokenValidationError(
                "invalid_sites", "allowed_sites claim must be a list of site IDs"
            )
        if not all(isinstance(site, str) and site for site in site_ids_raw):
            raise TokenValidationError("invalid_sites", "allowed_sites must contain site IDs")
        site_ids = frozenset(site_ids_raw)
    else:
        # Absent claim = all sites in tenant (documented contract).
        site_ids: frozenset[str] | None = None

    return CallerIdentity(
        subject=sub, tenant=tenant, roles=roles, site_ids=site_ids
    )
