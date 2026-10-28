"""Single-owner, bounded batch scheduling with explicit result acknowledgements.

The live composition root supplies synchronization and a monotonic clock. No
backend or callback executes inside this scheduler. A camera stays in flight
until its consumer acknowledges delivery, including tracking and event work.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import isfinite

from .batch import (
    BatchDelivery,
    BatchItem,
    BatchItemResult,
    BatchKey,
    DetectionBatch,
    FailureCode,
    ItemFailure,
)


@dataclass(frozen=True)
class Admission:
    accepted: bool
    reason: str
    displaced: BatchItem | None = None


@dataclass(frozen=True)
class SchedulerSnapshot:
    candidates: int
    in_flight: int
    awaiting_delivery: int
    oldest_candidate_age_s: float
    stopping: bool


class BatchScheduler:
    """Fair size-or-deadline scheduler over a fixed set of configured cameras."""

    def __init__(
        self,
        camera_ids: Iterable[str],
        max_batch_size: int = 16,
        max_wait_ms: float = 20,
        shutdown_timeout_s: float = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cameras = tuple(camera_ids)
        if not cameras or any(not camera for camera in cameras):
            raise ValueError("at least one non-empty camera ID required")
        if len(cameras) != len(set(cameras)):
            raise ValueError("camera IDs must be unique")
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int):
            raise TypeError("maximum batch size must be a positive integer")
        if max_batch_size <= 0:
            raise ValueError("maximum batch size must be positive")
        if not isfinite(max_wait_ms) or max_wait_ms <= 0:
            raise ValueError("maximum wait must be finite and positive")
        if not isfinite(shutdown_timeout_s) or shutdown_timeout_s <= 0:
            raise ValueError("shutdown timeout must be finite and positive")
        self._cameras = cameras
        self._sessions: dict[str, str | None] = dict.fromkeys(cameras)
        self._last: dict[str, tuple[int, float]] = {}
        self._candidates: dict[str, tuple[BatchItem, float]] = {}
        self._in_flight: dict[str, BatchItem] = {}
        self._deliveries: dict[str, BatchDelivery] = {}
        self._active: DetectionBatch | None = None
        self._cursor = 0
        self._max_batch_size = max_batch_size
        self._max_wait_s = max_wait_ms / 1000
        self._shutdown_timeout_s = shutdown_timeout_s
        self._stop_deadline: float | None = None
        self._clock = clock
        self._last_clock = float("-inf")

    def _now(self) -> float:
        now = self._clock()
        if not isfinite(now) or now < self._last_clock:
            raise ValueError("scheduler clock must be finite and monotonic")
        self._last_clock = now
        return now

    def _check_camera(self, camera_id: str) -> None:
        if camera_id not in self._sessions:
            raise ValueError(f"unknown camera: {camera_id}")

    def start_session(self, camera_id: str, session_id: str) -> BatchItem | None:
        """Invalidate a candidate on reconnect; outstanding execution stays owned.

        The caller resets that camera's tracker/rules before delivering the new
        session. Old execution must resolve before another item can be admitted.
        """
        self._check_camera(camera_id)
        if not session_id:
            raise ValueError("session ID required")
        if self._stop_deadline is not None:
            raise RuntimeError("scheduler is stopping")
        if self._sessions[camera_id] == session_id:
            return None
        self._sessions[camera_id] = session_id
        self._last.pop(camera_id, None)
        candidate = self._candidates.pop(camera_id, None)
        return candidate[0] if candidate else None

    def offer(self, item: BatchItem) -> Admission:
        """Keep at most one candidate per camera; report every displacement."""
        camera = item.key.camera_id
        self._check_camera(camera)
        now = self._now()
        if self._stop_deadline is not None:
            return Admission(False, "stopping")
        if self._sessions[camera] != item.key.session_id:
            return Admission(False, "stale_session")
        last = self._last.get(camera)
        if last and (item.key.frame_index <= last[0] or item.frame.timestamp_s < last[1]):
            return Admission(False, "out_of_order")
        if item.enqueued_at > now:
            raise ValueError("frame enqueue time lies in the future")
        previous = self._candidates.get(camera)
        # Replacement does not reset the oldest-candidate deadline.
        self._candidates[camera] = (item, previous[1] if previous else now)
        self._last[camera] = (item.key.frame_index, item.frame.timestamp_s)
        return Admission(True, "replaced" if previous else "accepted",
                         previous[0] if previous else None)

    def next_batch(self) -> DetectionBatch | None:
        """Return one full or expired partial batch; never overlap executions."""
        now = self._now()
        if self._stop_deadline is not None or self._active is not None:
            return None
        ready = {
            camera: value for camera, value in self._candidates.items()
            if camera not in self._in_flight
        }
        if not ready:
            return None
        oldest = min(value[1] for value in ready.values())
        if len(ready) < self._max_batch_size and now - oldest < self._max_wait_s:
            return None
        selected = []
        for offset in range(len(self._cameras)):
            index = (self._cursor + offset) % len(self._cameras)
            camera = self._cameras[index]
            if camera in ready:
                selected.append(self._candidates.pop(camera)[0])
                next_cursor = (index + 1) % len(self._cameras)
                if len(selected) == self._max_batch_size:
                    break
        self._cursor = next_cursor
        batch = DetectionBatch(tuple(selected))
        self._active = batch
        self._in_flight.update((item.key.camera_id, item) for item in batch.items)
        return batch

    def complete(
        self, batch: DetectionBatch, results: Iterable[BatchItemResult],
    ) -> tuple[BatchDelivery, ...]:
        """Validate the entire response atomically and correlate by identity.

        The caller must check the current session again immediately before
        processing a returned delivery and acknowledge after downstream work.
        """
        if batch is not self._active:
            raise ValueError("batch is not the active execution")
        expected = {item.key for item in batch.items}
        by_key = {}
        for result in results:
            if result.key not in expected:
                raise ValueError("result identities do not match submitted items")
            if result.key in by_key:
                raise ValueError("duplicate result identity")
            by_key[result.key] = result
        if set(by_key) != expected:
            raise ValueError("result identities do not match submitted items")
        deliveries = []
        for item in batch.items:
            result = by_key[item.key]
            if not self.is_current(item.key):
                result = BatchItemResult(item.key, failure=ItemFailure(
                    FailureCode.STALE_SESSION, "session changed during execution"))
            delivery = BatchDelivery(item, result)
            self._deliveries[item.key.camera_id] = delivery
            deliveries.append(delivery)
        self._active = None
        return tuple(deliveries)

    def fail_batch(self, batch: DetectionBatch, message: str) -> tuple[BatchDelivery, ...]:
        """Resolve all submitted items explicitly after an executor failure."""
        failure = ItemFailure(FailureCode.BATCH, message)
        return self.complete(batch, (BatchItemResult(item.key, failure=failure)
                                     for item in batch.items))

    def is_current(self, key: BatchKey) -> bool:
        self._check_camera(key.camera_id)
        return self._sessions[key.camera_id] == key.session_id

    def acknowledge(self, key: BatchKey) -> None:
        """Release a camera only after its result was delivered or discarded."""
        self._check_camera(key.camera_id)
        delivery = self._deliveries.get(key.camera_id)
        if delivery is None or delivery.item.key != key:
            raise ValueError("no matching result awaiting acknowledgement")
        del self._deliveries[key.camera_id]
        del self._in_flight[key.camera_id]

    def stop(self) -> tuple[BatchDelivery, ...]:
        """Cancel unsubmitted candidates and start the bounded drain interval."""
        now = self._now()
        if self._stop_deadline is not None:
            return ()
        self._stop_deadline = now + self._shutdown_timeout_s
        cancelled = tuple(BatchDelivery(item, BatchItemResult(
            item.key, failure=ItemFailure(FailureCode.CANCELLED, "stopped before submission")))
            for item, _ in self._candidates.values())
        self._candidates.clear()
        return cancelled

    def expire_shutdown(self) -> tuple[BatchDelivery, ...]:
        """Report and release unresolved items at the deadline, without blocking.

        This abandons logical work; the runtime must terminate/join its own
        executor separately. Late executor results are rejected by complete().
        """
        now = self._now()
        if self._stop_deadline is None or now < self._stop_deadline:
            return ()
        abandoned = tuple(BatchDelivery(item, BatchItemResult(
            item.key, failure=ItemFailure(FailureCode.SHUTDOWN_TIMEOUT,
                                         "shutdown deadline reached")))
            for item in self._in_flight.values())
        self._in_flight.clear()
        self._deliveries.clear()
        self._active = None
        return abandoned

    def snapshot(self) -> SchedulerSnapshot:
        now = self._now()
        age = max((now - admitted for _, admitted in self._candidates.values()), default=0.0)
        return SchedulerSnapshot(len(self._candidates), len(self._in_flight),
                                 len(self._deliveries), age, self._stop_deadline is not None)
