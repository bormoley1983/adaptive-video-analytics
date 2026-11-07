"""Request logging and request-ID middleware for the AVA API.

Each request gets one trace ID: a valid client-supplied UUID is preserved,
otherwise AVA generates one. The ID is returned on every response via
``X-Request-Id`` and appears in the single per-request log line. A malformed
supplied value is rejected before routing with an RFC 9457 problem response;
the offending value is never reflected or logged.

The log record includes: request ID, correlation ID (when set by the
idempotency middleware), idempotency key (when present), method, route
template, status, and duration. Bodies are omitted because no fields are
approved for logging.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .request_id import (
    REQUEST_ID_HEADER,
    InvalidRequestIdError,
    generate_request_id,
    parse_request_id,
)

logger = logging.getLogger("ava.api.request")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Resolve the request ID and emit one structured log line per request.

    The Authorization header is never included in the log record. A malformed
    ``X-Request-Id`` short-circuits to a 400 problem response whose body and
    headers carry only a freshly generated trace ID.

    Log fields:
    - request_id: always present (client-supplied or generated).
    - correlation_id: set by IdempotencyMiddleware (outer); logged when present.
    - idempotency_key: set by IdempotencyMiddleware; logged when present.
    - method, path (route template), status, duration_ms.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            request_id = parse_request_id(
                request.headers.get(REQUEST_ID_HEADER)
            )
        except InvalidRequestIdError:
            request_id = generate_request_id()
            from .idempotency_middleware import _is_canonical_uuid
            supplied = request.headers.get("X-Correlation-Id", "")
            correlation_id = supplied.lower() if _is_canonical_uuid(supplied) else str(uuid.uuid4())
            logger.info(
                "method=%s path=%s status=400 duration_ms=0 request_id=%s correlation_id=%s "
                "idempotency_key=- rejected=invalid_request_id",
                request.method, "<unmatched>", request_id, correlation_id,
            )
            response = self._invalid_request_id_response(request_id)
            response.headers["X-Correlation-Id"] = correlation_id
            return response

        request.state.request_id = request_id
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - sanitize failures at the HTTP boundary
            # Keep internal exceptions and request bodies out of public errors.
            response = JSONResponse(
                status_code=500,
                content={"type": "about:blank", "title": "Internal Server Error",
                         "status": 500, "code": "internal_error", "request_id": request_id},
                media_type="application/problem+json",
            )
        duration_ms = (time.monotonic() - start) * 1000.0

        # Gather optional fields set by the idempotency middleware (outer).
        correlation_id = getattr(request.state, "correlation_id", None)
        idempotency_key = getattr(request.state, "idempotency_key", None)

        # Use the full request path (includes /api/v1 prefix) for logging.
        logger.info(
            "method=%s path=%s status=%d duration_ms=%.1f request_id=%s"
            " correlation_id=%s idempotency_key=%s",
            request.method,
            getattr(request.scope.get("route"), "path", "<unmatched>"),
            response.status_code,
            duration_ms,
            request_id,
            correlation_id or "-",
            idempotency_key or "-",
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers.setdefault("X-Correlation-Id", correlation_id or str(uuid.uuid4()))
        return response

    @staticmethod
    def _invalid_request_id_response(request_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "code": "invalid_request_id",
                "detail": (
                    "X-Request-Id must be a canonical RFC 4122 UUID; "
                    "the supplied value was rejected"
                ),
                "request_id": request_id,
            },
            headers={REQUEST_ID_HEADER: request_id},
            media_type="application/problem+json",
        )
