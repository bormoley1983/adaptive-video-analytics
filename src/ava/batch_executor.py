"""Bounded single model-owner execution with explicit native-call abandonment."""

from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic

from .batch import BatchItemResult, DetectionBatch, FailureCode, ItemFailure


@dataclass(frozen=True)
class Execution:
    batch: DetectionBatch
    results: tuple[BatchItemResult, ...]
    seconds: float


class BatchExecutor:
    """Load, invoke and release a backend on exactly one daemon thread."""

    def __init__(self, factory):
        self._factory = factory
        self._requests = Queue(maxsize=1)
        self._results = Queue(maxsize=1)
        self._stop = Event()
        self.ready = Event()
        self.error = None
        self.cleanup_error = None
        self._busy = False
        self._started = False
        self.backend = None
        self._thread = Thread(target=self._run, name="batch-model-owner", daemon=True)

    def start(self):
        if self._started:
            raise RuntimeError("executor already started")
        self._started = True
        self._thread.start()

    def submit(self, batch):
        if not self.ready.is_set() or self.error or self._stop.is_set() or self._busy:
            raise RuntimeError("executor is unavailable")
        self._requests.put_nowait(batch)
        self._busy = True

    def poll(self):
        try:
            result = self._results.get_nowait()
        except Empty:
            return None
        self._busy = False
        return result

    def close(self, timeout):
        if timeout < 0:
            raise ValueError("shutdown timeout must be non-negative")
        self._stop.set()
        if self._started:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self):
        try:
            self.backend = self._factory()
            self.backend.load()
            self.ready.set()
            while not self._stop.is_set() or not self._requests.empty():
                try:
                    batch = self._requests.get(timeout=.02)
                except Empty:
                    continue
                started = monotonic()
                try:
                    results = tuple(self.backend.detect_batch(batch))
                except Exception as exc:  # noqa: BLE001 - preserve worker failure and cleanup evidence
                    failure = ItemFailure(FailureCode.BATCH, str(exc) or type(exc).__name__)
                    results = tuple(BatchItemResult(item.key, failure=failure)
                                    for item in batch.items)
                try:
                    self._results.put_nowait(Execution(batch, results, monotonic() - started))
                except Full:
                    raise RuntimeError("undelivered executor result") from None
        except Exception as exc:  # noqa: BLE001 - preserve worker failure and cleanup evidence
            self.error = exc
            self.ready.set()
        finally:
            if self.backend is not None:
                try:
                    self.backend.close()
                except Exception as exc:  # noqa: BLE001 - preserve worker failure and cleanup evidence
                    self.cleanup_error = exc
