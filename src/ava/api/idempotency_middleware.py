"""Idempotency-Key validation and X-Correlation-Id middleware.

Enforces the idempotency contract at the presentation edge:

* ``Idempotency-Key`` is REQUIRED on all routes except ``/health/live`` and
  ``/health/ready``. Missing key → 400 ``idempotency_key_missing``.
  Malformed key (not a canonical hyphenated UUID) → 400
  ``invalid_idempotency_key``. Uppercase and lowercase hex are both accepted
  and normalized to lowercase internally before namespacing, storage, and
  logging.
* ``X-Correlation-Id``: if supplied and a valid canonical hyphenated UUID,
  it is preserved (normalized to lowercase). If absent, AVA generates one.
  A malformed supplied value → 400 ``invalid_correlation_id``. The value is
  returned on every response (success and error).

This middleware runs after request-ID resolution and BEFORE routing so that the key is validated
even for routes that are later rejected by auth or access checks.
"""

from __future__ import annotations

import logging
import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("ava.api.idempotency")

# Paths exempt from the Idempotency-Key requirement (health probes).
# Probes may omit the key but must supply a valid value if present.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/api/v1/health/live", "/api/v1/health/ready"})

# Canonical hyphenated UUID: 8-4-4-4-12 hex digits (upper or lowercase).
_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_canonical_uuid(value: str) -> bool:
    """Check for canonical hyphenated UUID form (upper or lowercase hex)."""
    return bool(_CANONICAL_UUID_RE.fullmatch(value))


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Validate Idempotency-Key and propagate X-Correlation-Id.

    RequestLoggingMiddleware wraps this middleware so validation failures
    receive request IDs and are included in the audit log.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Resolve X-Correlation-Id.
        # - Absent → generate a new one.
        # - Present and valid canonical UUID → normalize to lowercase, preserve.
        # - Present but malformed → 400 invalid_correlation_id.
        raw_corr = request.headers.get("X-Correlation-Id")
        if raw_corr is not None:
            if not _is_canonical_uuid(raw_corr):
                request.state.correlation_id = str(uuid.uuid4())
                return self._problem(
                    400, "invalid_correlation_id",
                    "X-Correlation-Id must be a canonical hyphenated UUID",
                    request.state.correlation_id, request.state.request_id,
                )
            correlation_id = raw_corr.lower()
        else:
            correlation_id = str(uuid.uuid4())

        request.state.correlation_id = correlation_id

        key = request.headers.get("Idempotency-Key")
        if key is None and path not in _EXEMPT_PATHS:
            return self._problem(400, "idempotency_key_missing",
                                 "Idempotency-Key is required on non-probe routes", correlation_id, request.state.request_id)
        if key is not None:
            if not _is_canonical_uuid(key):
                return self._problem(400, "invalid_idempotency_key",
                                     "Idempotency-Key must be a canonical UUID", correlation_id, request.state.request_id)
            request.state.idempotency_key = key.lower()

        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response

    def _problem(
        self, status: int, code: str, detail: str, correlation_id: str | None, request_id: str
    ) -> JSONResponse:
        title = {400: "Bad Request"}.get(status, "Error")
        headers: dict[str, str] = {}
        if correlation_id is not None:
            headers["X-Correlation-Id"] = correlation_id
        return JSONResponse(
            status_code=status,
            content={
                "type": "about:blank",
                "title": title,
                "status": status,
                "code": code,
                "detail": detail,
                "request_id": request_id,
            },
            headers=headers,
            media_type="application/problem+json",
        )
