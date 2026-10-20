"""Versioned event and evidence contract (ADR-5).

Every rule-triggered incident is recorded as a versioned JSON object with:

- a schema version, so readers can evolve without breaking old records;
- a stable event ID derived from (camera, session, track, rule, kind,
  occurrence), so retries over the same stream preserve the same IDs and
  downstream consumers can deduplicate idempotently;
- camera/session/track/rule identifiers, class label and detector confidence;
- source time (PTS of the triggering frame) and emission time (wall-clock
  UTC);
- config/model references so a record is interpretable without the run host;
- an evidence locator that resolves to the concrete snapshot or clip for the
  incident.

The writer appends one JSON object per line to ``events.jsonl`` and, when
evidence is requested by a rule, copies the triggering frame (snapshot) or a
bounded window of frames (clip) into the run's evidence directory.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .rules import RuleEvent


def _safe_event_filename(event_id: str) -> str:
    """Map an event ID to a filesystem-safe filename stem.

    Event IDs contain '/' as a namespace separator (camera/session/track/...);
    on disk we flatten those to dots so the evidence path stays a single file
    name under ``evidence/<camera>/``.
    """
    return event_id.replace("/", ".")


@dataclass(frozen=True)
class EvidenceLocator:
    """Where an event's evidence lives on disk.

    ``kind`` is ``"snapshot"`` (single frame image), ``"clip"`` (short video)
    or ``None`` when the rule did not request evidence. ``path`` is relative
    to the run output directory so records stay portable.
    """

    kind: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class EventRecord:
    """The full ADR-5 event record as persisted to JSONL."""

    schema_version: str
    event_id: str
    camera_id: str
    session_id: str
    track_id: int
    rule_id: str
    rule_type: str
    class_label: str
    confidence: float
    source_time_s: float
    emitted_utc: str
    config_version: str
    model_ref: str | None
    evidence: EvidenceLocator
    frame_index: int = -1
    payload: dict = field(default_factory=dict)

    @classmethod
    def from_rule_event(cls, event: RuleEvent, *, config_version: str,
                        model_ref: str | None,
                        evidence: EvidenceLocator | None = None) -> "EventRecord":
        return cls(
            schema_version=event.schema_version,
            event_id=event.event_id,
            camera_id=event.camera_id,
            session_id=event.session_id,
            track_id=event.track_id,
            rule_id=event.rule_id,
            rule_type=event.rule_type,
            class_label=event.class_label,
            confidence=event.confidence,
            source_time_s=event.source_time_s,
            emitted_utc=event.emitted_utc,
            config_version=config_version,
            model_ref=model_ref,
            evidence=evidence or EvidenceLocator(),
            frame_index=event.frame_index,
            payload=dict(event.payload),
        )

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, sort_keys=True)


class EventWriter:
    """Append-only JSONL writer for versioned event records.

    Idempotency: writing the same ``event_id`` twice appends two lines; the
    *contract* guarantee is that a retry over the same stream produces the
    same IDs, so consumers deduplicate on ``event_id``. The writer itself
    tracks seen IDs and can optionally skip duplicates (``dedupe=True``) for
    crash-recovery runs where the file may already contain partial output.
    """

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

    def close(self) -> None:
        self._handle.close()


class EvidenceStore:
    """Materializes evidence for events into the run output directory.

    Snapshots copy the triggering BGR frame to ``evidence/<camera>/<event_id>.jpg``.
    Clips are bounded windows of frames encoded to a short MP4; the window is
    centered on the triggering frame and capped by the rule's ``clip_seconds``
    (converted to frames via the source fps).
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.evidence_dir = output_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def save_snapshot(self, camera_id: str, event_id: str, bgr_frame) -> EvidenceLocator:
        import cv2  # deferred: heavy import

        dest_dir = self.evidence_dir / camera_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        rel = f"evidence/{camera_id}/{_safe_event_filename(event_id)}.jpg"
        if not cv2.imwrite(str(self.output_dir / rel), bgr_frame):
            raise RuntimeError(f"failed to write snapshot: {rel}")
        return EvidenceLocator(kind="snapshot", path=rel)

    def save_clip(self, camera_id: str, event_id: str, frames: list[tuple[int, Any]],
                  fps: float, clip_seconds: float) -> EvidenceLocator:
        """Encode a bounded window of (frame_index, bgr_frame) pairs to MP4.

        ``frames`` should be pre-sliced to the desired window by the caller;
        this method only enforces the duration cap and writes the file.
        """
        import cv2  # deferred: heavy import

        max_frames = max(1, int(round(fps * clip_seconds)))
        frames = frames[:max_frames]
        if not frames:
            return EvidenceLocator()
        dest_dir = self.evidence_dir / camera_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        rel = f"evidence/{camera_id}/{_safe_event_filename(event_id)}.mp4"
        h, w = frames[0][1].shape[:2]
        writer = cv2.VideoWriter(str(self.output_dir / rel),
                                 cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if not writer.isOpened():
            raise RuntimeError(f"cannot open clip writer: {rel}")
        for _, bgr in frames:
            writer.write(bgr)
        writer.release()
        return EvidenceLocator(kind="clip", path=rel)
