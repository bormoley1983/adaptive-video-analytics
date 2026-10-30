"""Request logging and request-ID middleware for the AVA API.

Each request gets one trace ID: a valid client-supplied UUID is preserved,
otherwise AVA generates one. The ID is returned on every response via
``X-Request-Id`` and appears in the single per-request log line. A malformed
supplied value is rejected before routing with an RFC 9457 problem response;
the offending value is never reflected or logged.
"""

from __future__ import annotations

import logging
import time

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
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            request_id = parse_request_id(
                request.headers.get(REQUEST_ID_HEADER)
            )
        except InvalidRequestIdError:
            request_id = generate_request_id()
            logger.info(
                "method=%s path=%s status=400 rejected=invalid_request_id",
                request.method,
                request.url.path,
            )
            return self._invalid_request_id_response(request_id)

        request.state.request_id = request_id
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000.0

        logger.info(
            "method=%s path=%s status=%d duration_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        response.headers[REQUEST_ID_HEADER] = request_id
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
