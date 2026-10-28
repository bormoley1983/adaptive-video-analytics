"""Outbound persistence ports (ADR-16)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..events import EventRecord


@runtime_checkable
class EventSink(Protocol):
    """Outbound port: accept a canonical event record (idempotent on event_id)."""

    def write(self, record: EventRecord) -> bool:
        """Persist one record. Returns False if skipped as a duplicate."""
        ...

    def has_event(self, event_id: str) -> bool:
        """Return True if an event with this ID already exists."""
        ...

    def close(self) -> None:
        """Flush and release underlying resources."""
        ...


class EventRepository(Protocol):
    """Outbound port: query immutable events (read side)."""

    def find_by_event_id(self, event_id: str) -> EventRecord | None:
        ...

    def list_by_camera(
        self, camera_id: str, *, before: float | None = None, limit: int = 100
    ) -> list[EventRecord]:
        ...


class UnitOfWork(Protocol):
    """Outbound port: atomically commit domain state and outbox intent."""

    def begin(self) -> None:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...
