"""JSONL event sink adapter (ADR-16)."""

from __future__ import annotations

import json
from pathlib import Path

from ..events import EventRecord


class JsonlEventSink:
    """Append-only JSONL writer. Optional deduplication on event_id."""

    def __init__(self, path: Path, *, dedupe: bool = False) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dedupe = dedupe
        self._seen: set[str] = set()
        if dedupe and path.exists():
            # Rebuild the seen set from any existing output (crash recovery).
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._seen.add(json.loads(line)["event_id"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        self._handle = path.open("a", encoding="utf-8")

    def write(self, record: EventRecord) -> bool:
        """Append one record. Returns False if skipped as a duplicate."""
        if self._dedupe and record.event_id in self._seen:
            return False
        self._seen.add(record.event_id)
        self._handle.write(record.to_json() + "\n")
        self._handle.flush()
        return True

    def has_event(self, event_id: str) -> bool:
        """JSONL is append-only; a pre-existing file may contain the ID."""
        return self._dedupe and event_id in self._seen

    def close(self) -> None:
        self._handle.close()
