"""Correlated detection contracts, independent of model and tracker backends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Protocol

from .detector import Detection, FrameResult
from .ingest import Frame


@dataclass(frozen=True)
class BatchKey:
    camera_id: str
    session_id: str
    frame_index: int

    def __post_init__(self) -> None:
        if (not isinstance(self.camera_id, str) or not self.camera_id
                or not isinstance(self.session_id, str) or not self.session_id
                or isinstance(self.frame_index, bool) or not isinstance(self.frame_index, int)
                or self.frame_index < 0):
            raise ValueError("camera/session identity and non-negative frame index required")


@dataclass(frozen=True)
class BatchItem:
    key: BatchKey
    frame: Frame
    enqueued_at: float

    def __post_init__(self) -> None:
        if self.frame.index != self.key.frame_index or self.frame.session_id != self.key.session_id:
            raise ValueError("frame identity differs from batch identity")
        if not isfinite(self.frame.timestamp_s) or self.frame.timestamp_s < 0:
            raise ValueError("source timestamp must be finite and non-negative")
        if not isfinite(self.enqueued_at):
            raise ValueError("enqueue time must be finite")


@dataclass(frozen=True)
class DetectionBatch:
    items: tuple[BatchItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not self.items:
            raise ValueError("batch requires a non-empty immutable tuple")
        cameras = [item.key.camera_id for item in self.items]
        if len(cameras) != len(set(cameras)):
            raise ValueError("a batch permits only one item per camera")


class FailureCode(str, Enum):
    ITEM = "item_failed"
    BATCH = "batch_failed"
    STALE_SESSION = "stale_session"
    CANCELLED = "cancelled"
    SHUTDOWN_TIMEOUT = "shutdown_timeout"


@dataclass(frozen=True)
class ItemFailure:
    code: FailureCode
    message: str

    def __post_init__(self) -> None:
        if (not isinstance(self.code, FailureCode)
                or not isinstance(self.message, str) or not self.message):
            raise ValueError("typed failure code and message required")


@dataclass(frozen=True)
class BatchItemResult:
    key: BatchKey
    width: int = 0
    height: int = 0
    detections: tuple[Detection, ...] = ()
    failure: ItemFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.detections, tuple):
            raise TypeError("detections must be an immutable tuple")
        if self.failure is not None:
            if not isinstance(self.failure, ItemFailure):
                raise TypeError("failure must carry a typed ItemFailure")
            if self.detections or self.width or self.height:
                raise ValueError("failed result cannot contain detection metadata")
        elif (any(isinstance(value, bool) or not isinstance(value, int)
                  for value in (self.width, self.height))
              or self.width <= 0 or self.height <= 0):
            raise ValueError("successful result requires positive source dimensions")
        for detection in self.detections:
            if detection.track_id is not None:
                raise ValueError("batch detection must not carry tracker state")
            x1, y1, x2, y2 = detection.box
            if not all(isfinite(v) and 0 <= v <= 1 for v in detection.box):
                raise ValueError("detection boxes must be finite normalized source coordinates")
            if x2 < x1 or y2 < y1:
                raise ValueError("detection box coordinates are reversed")
            if not isfinite(detection.confidence) or not 0 <= detection.confidence <= 1:
                raise ValueError("detection confidence must lie in [0, 1]")


@dataclass(frozen=True)
class BatchDelivery:
    item: BatchItem
    result: BatchItemResult


class BatchDetector(Protocol):
    """One executor owns this stateless detection backend."""

    def detect_batch(self, batch: DetectionBatch) -> tuple[BatchItemResult, ...]: ...


class CameraTracker(Protocol):
    """One instance per camera; a session reset never touches another camera."""

    def reset(self, session_id: str) -> None: ...

    def update(self, item: BatchItem, result: BatchItemResult) -> FrameResult: ...
