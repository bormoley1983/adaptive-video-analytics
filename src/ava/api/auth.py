"""Bearer-token authentication for the AVA API.

Provides the FastAPI dependency that extracts a Bearer token from the
Authorization header, verifies it through the IdentityPolicy port, and
attaches the resulting CallerIdentity to the request state.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..application.identity import CallerIdentity, IdentityPolicy, TokenValidationError


@dataclass(frozen=True)
class AuthConfig:
    """Authentication configuration for the API application."""

    issuer: str
    jwks_uri: str


def create_auth_dependency(policy: IdentityPolicy):
    """Build a FastAPI dependency that verifies bearer tokens.

    Returns a callable suitable for ``Depends(...)`` in route signatures.
    The verified CallerIdentity is stored on ``request.state.caller``.
    """

    def get_caller_identity(request: Request) -> CallerIdentity:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            reject_authentication("missing_token", "Authorization header with Bearer token required")

        token = auth_header[len("Bearer "):]
        try:
            caller = policy.verify(token)
        except TokenValidationError as exc:
            reject_authentication(exc.code, exc.message)

        request.state.caller = caller
        return caller

    return get_caller_identity


class AuthenticationError(HTTPException):
    """An authentication failure rendered as a problem response."""


def reject_authentication(code: str, detail: str) -> None:
    """Raise an HTTP 401 with an RFC 9457 problem body."""
    raise AuthenticationError(
        status_code=401,
        detail={"type": "about:blank", "title": "Unauthorized", "status": 401,
                "code": code, "detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def problem_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail,
                        headers=exc.headers, media_type="application/problem+json")
