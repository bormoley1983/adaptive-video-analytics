"""Request-ID resolution and propagation for the AVA API.

Every response carries ``X-Request-Id``. A client-supplied value is preserved
only when it is a canonical RFC 4122 UUID; anything else is rejected with an
RFC 9457 problem response carrying a freshly generated trace ID. The malformed
value is never reflected in the response body or written to logs.
"""

from __future__ import annotations

import re
import uuid

REQUEST_ID_HEADER = "X-Request-Id"

# RFC 4122 canonical form: lowercase hex, fixed hyphen positions. \Z (not $)
# so a trailing newline cannot slip past the anchor.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


class InvalidRequestIdError(Exception):
    """A supplied request ID is not a canonical UUID.

    Carries no client-supplied text: the malformed value must never be
    reflected in responses or logs, so it is intentionally not retained.
    """


def parse_request_id(value: str | None) -> str:
    """Resolve the effective request ID for one request.

    Args:
        value: Raw ``X-Request-Id`` header value, or ``None`` when absent.

    Returns:
        The client-supplied UUID when valid, otherwise a generated UUID.

    Raises:
        InvalidRequestIdError: When the supplied value is not a canonical
            lowercase RFC 4122 UUID.
    """
    if value is None:
        return str(uuid.uuid4())
    if _UUID_RE.match(value):
        return value
    raise InvalidRequestIdError()


def generate_request_id() -> str:
    """Generate a fresh trace ID for requests without a usable header."""
    return str(uuid.uuid4())
