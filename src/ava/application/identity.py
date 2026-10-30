"""Identity and authorization contracts for the AVA API.

Defines the IdentityPolicy port that maps verified token claims to a
CallerIdentity the presentation layer attaches to each request. The
application layer owns this contract; infrastructure adapters implement it.
No PyJWT, httpx, or FastAPI imports here (ADR-16 dependency direction).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TokenValidationError(Exception):
    """Raised when a bearer token fails verification.

    Attributes:
        code: Machine-readable failure code for RFC 9457 problem mapping.
        message: Human-readable summary (no secrets, no claim values).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CallerIdentity:
    """Verified caller context derived from token claims."""

    subject: str
    tenant: str
    roles: frozenset[str]
    site_ids: frozenset[str] | None = None  # None = all sites in tenant


# Roles recognised by the AVA authorization model (api-contract.md §2.2).
KNOWN_ROLES: frozenset[str] = frozenset(
    {"viewer", "operator", "configurator", "integrator", "auditor", "admin"}
)


class IdentityPolicy(Protocol):
    """Port for token verification and claims-to-identity mapping.

    Implementations decode, validate (signature, issuer, expiry, required
    claims) and map a raw bearer token to a CallerIdentity.
    """

    def verify(self, token: str) -> CallerIdentity:
        """Verify *token* and return the caller's identity.

        Raises:
            TokenValidationError: if the token is missing, malformed,
                expired, from the wrong issuer, or lacks required claims.
        """
        ...
