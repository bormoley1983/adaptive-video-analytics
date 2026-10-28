"""Composition root: selects the EventSink adapter from config."""

from __future__ import annotations

from pathlib import Path

from ..config import PersistenceConfig
from .jsonl_sink import JsonlEventSink
from .ports import EventSink


def build_event_sink(
    persistence: PersistenceConfig,
    *,
    jsonl_path: Path | None = None,
) -> EventSink:
    """Construct the configured event sink. Raises ValueError on missing DSN/path."""
    if persistence.backend == "postgres":
        if not persistence.dsn:
            raise ValueError(
                "persistence.dsn is required when persistence.backend is 'postgres' "
                "(set AVA_PG_DSN or provide persistence.dsn in the config)"
            )
        from .postgres import PostgresEventSink

        sink = PostgresEventSink(persistence.dsn)
        try:
            sink.check_schema()
        except Exception:
            sink.close()
            raise
        return sink

    if jsonl_path is None:
        raise ValueError(
            "jsonl_path is required when persistence.backend is 'jsonl'"
        )
    return JsonlEventSink(jsonl_path)
