"""Request logging middleware for the AVA API.

Logs one line per request (method, path, status, duration) with the
Authorization header stripped so no token material reaches the log stream.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("ava.api.request")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Emit a single structured log line per request.

    The Authorization header is never included in the log record.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000.0

        logger.info(
            "method=%s path=%s status=%d duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
