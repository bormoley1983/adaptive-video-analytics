import threading
import time

import pytest

from ava.batch import BatchItem, BatchItemResult, BatchKey, DetectionBatch
from ava.batch_executor import BatchExecutor
from ava.ingest import Frame


def batch():
    return DetectionBatch((BatchItem(BatchKey("a", "s", 0), Frame(0, 0., None, session_id="s"),
                                     time.monotonic()),))


class Backend:
    def __init__(self, fail=False):
        self.threads = []
        self.fail = fail
        self.closed = False

    def load(self):
        self.threads.append(threading.get_ident())

    def detect_batch(self, value):
        self.threads.append(threading.get_ident())
        if self.fail:
            raise ValueError("model failed")
        return tuple(BatchItemResult(i.key, 10, 10) for i in value.items)

    def close(self):
        self.threads.append(threading.get_ident())
        self.closed = True


def wait_result(executor):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = executor.poll()
        if result is not None:
            return result
        time.sleep(.001)
    pytest.fail("executor did not return")


@pytest.mark.parametrize("fail", [False, True])
def test_executor_owns_lifecycle_and_correlates_failures(fail):
    backend = Backend(fail)
    executor = BatchExecutor(lambda: backend)
    executor.start()
    assert executor.ready.wait(2)
    submitted = batch()
    executor.submit(submitted)
    with pytest.raises(RuntimeError):
        executor.submit(submitted)
    result = wait_result(executor)
    assert result.batch is submitted
    assert result.results[0].key == submitted.items[0].key
    assert bool(result.results[0].failure) == fail
    assert executor.close(2)
    assert backend.closed
    assert len(set(backend.threads)) == 1
    assert backend.threads[0] != threading.get_ident()


def test_load_failure_is_observable_and_backend_closes():
    backend = Backend()
    def fail():
        raise ValueError("load failed")
    backend.load = fail
    executor = BatchExecutor(lambda: backend)
    executor.start()
    assert executor.ready.wait(2)
    assert executor.close(2)
    assert isinstance(executor.error, ValueError)
    assert backend.closed


def test_native_call_shutdown_is_bounded():
    release = threading.Event()
    entered = threading.Event()
    backend = Backend()
    def detect(value):
        entered.set()
        release.wait(2)
        return ()
    backend.detect_batch = detect
    executor = BatchExecutor(lambda: backend)
    executor.start()
    assert executor.ready.wait(2)
    executor.submit(batch())
    assert entered.wait(2)
    assert not executor.close(.01)
    release.set()
    assert executor.close(2)


def test_rejects_submission_before_start_and_repeated_start():
    executor = BatchExecutor(Backend)
    with pytest.raises(RuntimeError):
        executor.submit(batch())
    executor.start()
    with pytest.raises(RuntimeError):
        executor.start()
    assert executor.close(2)


def test_cleanup_failure_remains_observable():
    backend = Backend()
    def close():
        raise ValueError("cleanup failed")
    backend.close = close
    executor = BatchExecutor(lambda: backend)
    executor.start()
    assert executor.ready.wait(2)
    assert executor.close(2)
    assert str(executor.cleanup_error) == "cleanup failed"
    with pytest.raises(ValueError, match="non-negative"):
        executor.close(-1)
