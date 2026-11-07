"""Ports and orchestration for atomic mutations and recoverable retries.

The transaction adapter owns persistence and locking. Callers authorize
before invoking the workflow; completed retries replay before preconditions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class TargetNotFound(Exception):
    """The mutation target has never existed."""


class IdempotencyKeyReused(Exception):
    """The caller reused a key for a different logical operation."""

    code = "idempotency_key_reused"


class RequestInProgress(Exception):
    """A mutation is waiting for ownership or exceeds available capacity."""

    code = "request_in_progress"


class PreconditionFailed(Exception):
    """The target representation failed a supplied precondition."""

    def __init__(self, current_etag: str | None) -> None:
        msg = (
            f"If-Match does not match current revision {current_etag!r}"
            if current_etag is not None
            else "If-Match precondition failed: target has no representation"
        )
        super().__init__(msg)
        self.code = "precondition_failed"
        self.current_etag = current_etag


class InvalidIfMatch(Exception):
    """The supplied conditional-header syntax is malformed."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = "invalid_if_match"


@dataclass(frozen=True)
class MutationResult:
    """Committed response metadata, excluding per-attempt identifiers."""

    status_code: int
    body: Any | None = None
    etag: str | None = None
    is_replay: bool = False


@dataclass(frozen=True)
class TargetState:
    """Target representation and validator from one transactional snapshot."""

    exists: bool
    revision: int = 0
    etag: str | None = None
    data: Any | None = None


@dataclass(frozen=True)
class MutationScope:
    """Serialization identity shared by operations that affect related state."""

    key: str


@runtime_checkable
class MutationTransaction(Protocol):
    """Port for the idempotent mutation transaction boundary.

    Implementations (PostgreSQL adapter) own the connection and transaction.
    All operations execute within a single database transaction on a single
    connection. The application layer orchestrates through this interface;
    it never touches SQL or connections directly.

    Lifecycle: ``begin`` → … operations … → ``commit`` or ``rollback``.
    After commit or rollback the transaction is closed and must not be reused.
    """

    async def begin(self) -> None:
        """Acquire a connection from the mutation pool and start a transaction."""
        ...

    async def claim_key(
        self, namespace: str, key_hash: str, fingerprint: str
    ) -> tuple[bool, MutationResult | None]:
        """Claim the idempotency key within the transaction.

        Returns:
            (True, None): new claim acquired; proceed with mutation.
            (False, result): completed record found; return the replay.

        Raises:
            IdempotencyKeyReused: key exists with a different fingerprint.
            RequestInProgress: concurrent duplicate detected (lock timeout).
        """
        ...

    async def lock_scope(self, scope: MutationScope) -> None:
        """Acquire the mutation-scope serialization lock.

        Uses a transaction-scoped advisory lock so that concurrent mutations
        for the same scope (including creation of an absent target) are
        serialized. The lock is held until commit or rollback.
        """
        ...

    async def read_target(self, camera_id: str) -> TargetState:
        """Read the current target state within the transaction.

        Returns a:class:`TargetState` snapshot with the revision and ETag.
        The ETag is computed by the adapter using the same resource-specific
        helper as GET responses, so validators are consistent.
        """
        ...

    async def apply_mutation(
        self, camera_id: str, operation: str, params: dict[str, Any]
    ) -> TargetState:
        """Apply the business mutation within the transaction.

        Args:
            camera_id: The target camera identifier.
            operation: One of ``"grant_put"``, ``"grant_delete"``,
                ``"camera_delete"``.
            params: Operation-specific parameters (e.g. site_id, tenant_ids).

        Returns:
            Updated:class:`TargetState` after the mutation and any revision
            increment. The handler advances the revision atomically when
            state changes; no-op operations leave it unchanged.
        """
        ...

    async def store_completed(
        self,
        key_hash: str,
        status_code: int,
        body: Any | None,
        etag: str | None,
    ) -> None:
        """Store the completed mutation result under the claimed key.

        Must be called before:meth:`commit` so that the response record and
        the state change commit atomically. A 204 may have a null body.
        """
        ...

    async def commit(self) -> None:
        """Commit the transaction, releasing all locks."""
        ...

    async def rollback(self) -> None:
        """Roll back the transaction, releasing all locks and any pending claim."""
        ...


class MutationTransactionFactory(Protocol):
    """Create request-local transactions backed by shared infrastructure."""

    def new_transaction(self, operation: str) -> MutationTransaction:
        ...


def compute_fingerprint(
    method: str,
    path: str,
    body: Any | None = None,
    if_match: str | None = None,
) -> str:
    """Compute the SHA-256 fingerprint for an idempotency key.

    The fingerprint covers the HTTP method, canonical path (including path
    parameters), sorted JSON request body, and the original If-Match header
    value when present. A fixed-length JSON tuple with sorted object keys
    and compact separators preserves boundaries between optional values.

    Two requests with the same key but different fingerprints are different
    logical operations (409 conflict).
    """
    canonical = json.dumps([method.upper(), path, body, if_match],
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Representation version for the current serialization format. Bump this when
# the wire format of GrantResponse or CameraState changes, which invalidates
# all previously issued validators ("every change to the selected
# representation data or variants must change its strong validator").
REPRESENTATION_VERSION = 1


def make_etag(
    resource_type: str,
    resource_id: str,
    representation_version: int,
    revision: int,
) -> str:
    """Build a resource-specific strong ETag (quoted opaque digest).

    The tag is a SHA-256 digest of the tuple
    ``(resource_type, resource_id, representation_version, revision)``,
    truncated to 32 hex characters and wrapped in double quotes per RFC 9110.

    Camera and grant tags differ even when they share the same internal
    camera revision, because the resource type differs. GET responses and
    mutation results use this same helper with consistent snapshots of
    representation data and revision.
    """
    canonical = json.dumps([resource_type, resource_id, representation_version, revision],
                           separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


def parse_if_match(value: str) -> list[str]:
    """Parse an If-Match header value for strong comparison.

    Returns:
        A list of full quoted strong ETag strings (one per comma-separated
        tag). For the wildcard ``*`` returns ``["*"]`` — a sentinel that
        :func:`etag_matches` evaluates against target existence rather than
        skipping the precondition entirely.

    Raises:
        InvalidIfMatch: When the header syntax is malformed.

    RFC 9110 §13.1.1: If-Match uses strong comparison. Weak validators
    (``W/"..."``) are valid syntax but cannot satisfy strong comparison.
    A comma-separated list of tags matches when any one tag matches the
    current representation. ``*`` matches when the target has any
    representation (exists); it does not match an absent target.
    """
    value = value.strip()
    if value == "*":
        return ["*"]

    # Scan quoted opaque tags; commas inside a tag are not separators.
    token_re = re.compile(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"')
    tags = []
    position = 0
    while position < len(value):
        match = token_re.match(value, position)
        if match is None:
            raise InvalidIfMatch("malformed If-Match list")
        token = match.group()
        if not token.startswith("W/"):
            tags.append(token)
        position = match.end()
        while position < len(value) and value[position] in " \t":
            position += 1
        if position == len(value):
            break
        if value[position] != ",":
            raise InvalidIfMatch("malformed If-Match separator")
        position += 1
        while position < len(value) and value[position] in " \t":
            position += 1
        if position == len(value):
            raise InvalidIfMatch("empty If-Match element")
    if not value:
        raise InvalidIfMatch("empty If-Match")
    return tags


def etag_matches(if_match_tags: list[str], current_etag: str | None) -> bool:
    """Evaluate If-Match against the current target ETag.

    Args:
        if_match_tags: Parsed If-Match (list of quoted tags, or ``["*"]``
            for the wildcard). Never empty — a missing header means no
            precondition and this function is not called.
        current_etag: The target's current strong ETag (quoted), or None
            when the target has no representation (absent resource).

    Returns:
        True when the precondition is satisfied.
    """
    if current_etag is None:
        # Absent target: neither * nor any tag can match.
        return False
    if "*" in if_match_tags:
        # Wildcard *: matches any existing representation.
        return True
    # Strong comparison: exact string match on the full quoted tag.
    return current_etag in if_match_tags


async def execute_idempotent_mutation(
    *,
    txn: MutationTransaction,
    namespace: str,
    key_hash: str,
    fingerprint: str,
    scope: MutationScope,
    camera_id: str,
    if_match_raw: str | None = None,
    operation: str,
    params: dict[str, Any],
) -> MutationResult:
    """Commit an operation and its retry result atomically.

    Matching completed claims replay before target-state preconditions.
    Failures and cancellation roll back the pending claim and state changes.
    """
    # Parse If-Match before the transaction (malformed → 400, no txn needed).
    if_match_tags: list[str] | None = None
    if if_match_raw is not None:
        if_match_tags = parse_if_match(if_match_raw)

    try:
        await txn.begin()

        claimed, replay = await txn.claim_key(namespace, key_hash, fingerprint)
        if not claimed:
            # Completed record found — return the stored response.
            # The replay carries the original status, body, and ETag.
            # It uses the current attempt's request/correlation IDs (set
            # by the presentation layer, not here).
            assert replay is not None
            # No state was written in this transaction; roll back to
            # release the connection cleanly (the claim row belongs to
            # the owner's committed transaction, not ours).
            await txn.rollback()
            return replay


        await txn.lock_scope(scope)


        target = await txn.read_target(camera_id)


        # if_match_tags is None only when no If-Match header was supplied
        # (no precondition); a present header always yields a non-empty list.
        if if_match_tags is not None and not etag_matches(if_match_tags, target.etag):
            raise PreconditionFailed(target.etag)


        updated = await txn.apply_mutation(camera_id, operation, params)


        result = _build_result(operation, updated)
        await txn.store_completed(key_hash, result.status_code, result.body, result.etag)


        await txn.commit()
        return result

    except BaseException:
        # Cancellation also rolls back the request-local transaction.
        # Any failure after claim: roll back (releases pending claim, locks).
        try:
            await asyncio.shield(txn.rollback())
        except Exception as rollback_err:  # noqa: BLE001
            logger.warning(
                "Best-effort rollback failed; pool will reclaim connection: %s",
                rollback_err,
            )
        raise


def _build_result(operation: str, target: TargetState) -> MutationResult:
    """Map committed resource state to the public mutation result."""
    if operation == "grant_put":

        body = target.data if isinstance(target.data, dict) else None
        etag = target.etag  # Grant ETag (resource_type="grant")
        return MutationResult(status_code=200, body=body, etag=etag)

    if operation == "grant_delete":

        return MutationResult(status_code=204, body=None, etag=None)

    if operation == "camera_delete":


        return MutationResult(status_code=204, body=None, etag=None)

    raise ValueError(f"unknown mutation operation: {operation!r}")


class MutationAdmission:
    """Bounded admission for concurrent mutations.

    Limits the number of in-flight mutation transactions to protect pool
    capacity for unrelated work. Excess requests are rejected immediately
    with:class:`RequestInProgress` (409) — they are not queued.

    The admission limit must not exceed the mutation pool's max_size.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("admission limit must be >= 1")
        self._limit = limit
        self._active = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self) -> bool:
        """Attempt to acquire an admission slot. Returns False if at limit."""
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        """Release an admission slot (on success, failure, or cancellation)."""
        if self._active > 0:
            self._active -= 1
