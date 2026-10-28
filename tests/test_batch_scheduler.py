"""Deterministic scheduling and lifecycle behavior with an injected clock."""

from dataclasses import replace

import pytest

from ava.batch import BatchItem, BatchItemResult, BatchKey, FailureCode, ItemFailure
from ava.batch_scheduler import BatchScheduler
from ava.ingest import Frame


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def item(camera="a", index=0, session="s1", timestamp=None):
    return BatchItem(BatchKey(camera, session, index),
                     Frame(index, index / 10 if timestamp is None else timestamp,
                           object(), session_id=session), 0.0)


def scheduler(clock, cameras=("a", "b", "c"), **options):
    result = BatchScheduler(cameras, clock=clock, **options)
    for camera in cameras:
        result.start_session(camera, "s1")
    return result


def successes(batch):
    return tuple(BatchItemResult(value.key, 640, 480) for value in batch.items)


def deliver(sched, batch):
    results = sched.complete(batch, successes(batch))
    for result in results:
        sched.acknowledge(result.item.key)
    return results


@pytest.mark.parametrize("options", [
    {"camera_ids": []}, {"camera_ids": [""]}, {"camera_ids": ["a", "a"]},
    {"max_batch_size": 0}, {"max_batch_size": -1}, {"max_batch_size": 1.5},
    {"max_batch_size": True}, {"max_wait_ms": 0}, {"max_wait_ms": float("nan")},
    {"max_wait_ms": float("inf")}, {"shutdown_timeout_s": 0},
    {"shutdown_timeout_s": float("nan")},
])
def test_rejects_invalid_scheduler_limits(options):
    with pytest.raises((TypeError, ValueError)):
        BatchScheduler(**({"camera_ids": ["a"]} | options))


def test_full_batch_flushes_immediately_in_camera_rotation_order(clock):
    sched = scheduler(clock, max_batch_size=2)
    sched.offer(item("b"))
    sched.offer(item("a"))

    batch = sched.next_batch()

    assert [value.key.camera_id for value in batch.items] == ["a", "b"]
    assert sched.next_batch() is None
    assert sched.snapshot().in_flight == 2


def test_sparse_batch_flushes_at_oldest_candidate_deadline(clock):
    sched = scheduler(clock)
    assert sched.next_batch() is None
    sched.offer(item())
    clock.now = 0.019
    assert sched.next_batch() is None

    clock.now = 0.020
    batch = sched.next_batch()

    assert len(batch.items) == 1
    assert batch.items[0].key.camera_id == "a"


def test_candidate_replacement_is_bounded_and_does_not_extend_deadline(clock):
    sched = scheduler(clock)
    original = item()
    sched.offer(original)
    clock.now = 0.019
    admission = sched.offer(item(index=1))

    assert admission.accepted
    assert admission.reason == "replaced"
    assert admission.displaced is original
    assert sched.snapshot().candidates == 1
    assert sched.snapshot().oldest_candidate_age_s == pytest.approx(0.019)
    clock.now = 0.020
    assert sched.next_batch().items[0].key.frame_index == 1


def test_round_robin_serves_sparse_camera_among_continuously_ready_cameras(clock):
    sched = scheduler(clock, max_batch_size=1)
    for camera in ("a", "b"):
        sched.offer(item(camera))
    first = sched.next_batch()
    deliver(sched, first)
    sched.offer(item("a", 1))
    sched.offer(item("c"))

    order = []
    for _ in range(2):
        batch = sched.next_batch()
        order.append(batch.items[0].key.camera_id)
        deliver(sched, batch)

    assert order == ["b", "c"]


def test_pending_delivery_blocks_only_its_camera(clock):
    sched = scheduler(clock, max_batch_size=1)
    sched.offer(item())
    batch = sched.next_batch()
    deliveries = sched.complete(batch, successes(batch))
    sched.offer(item(index=1))
    assert sched.next_batch() is None
    sched.offer(item("b"))

    other = sched.next_batch()
    assert other.items[0].key.camera_id == "b"
    deliver(sched, other)
    sched.acknowledge(deliveries[0].item.key)
    assert sched.next_batch().items[0].key.frame_index == 1


@pytest.mark.parametrize("candidate", [item(index=0), item(index=1, timestamp=0)])
def test_rejects_duplicate_or_regressing_source_order(clock, candidate):
    sched = scheduler(clock)
    sched.offer(item(index=0, timestamp=1))

    admission = sched.offer(candidate)

    assert not admission.accepted
    assert admission.reason == "out_of_order"
    assert sched.snapshot().candidates == 1


def test_equal_source_timestamp_with_increasing_index_is_accepted(clock):
    sched = scheduler(clock)
    sched.offer(item(timestamp=1))
    assert sched.offer(item(index=1, timestamp=1)).accepted


def test_offer_rejects_unstarted_or_stale_session(clock):
    sched = BatchScheduler(["a"], clock=clock)
    assert sched.offer(item()).reason == "stale_session"
    sched.start_session("a", "s2")
    assert not sched.offer(item()).accepted


def test_reconnect_cancels_candidate_and_discards_old_execution(clock):
    sched = scheduler(clock, max_batch_size=1)
    sched.offer(item())
    batch = sched.next_batch()
    pending = item(index=1)
    sched.offer(pending)

    assert sched.start_session("a", "s2") is pending
    assert sched.offer(item(session="s2")).accepted
    assert sched.next_batch() is None
    delivered = sched.complete(batch, successes(batch))

    assert delivered[0].result.failure.code == FailureCode.STALE_SESSION
    sched.acknowledge(delivered[0].item.key)
    assert sched.next_batch().items[0].key.session_id == "s2"


def test_repeated_session_notification_preserves_pending_candidate(clock):
    sched = scheduler(clock)
    sched.offer(item())
    assert sched.start_session("a", "s1") is None
    assert sched.snapshot().candidates == 1


def test_session_change_after_completion_is_visible_before_delivery(clock):
    sched = scheduler(clock, max_batch_size=1)
    sched.offer(item())
    batch = sched.next_batch()
    delivered = sched.complete(batch, successes(batch))
    sched.start_session("a", "s2")

    assert not sched.is_current(delivered[0].item.key)
    sched.acknowledge(delivered[0].item.key)
    assert sched.snapshot().in_flight == 0


def test_correlates_reordered_results_and_preserves_submission_order(clock):
    sched = scheduler(clock, max_batch_size=2)
    sched.offer(item("a"))
    sched.offer(item("b"))
    batch = sched.next_batch()

    delivered = sched.complete(batch, reversed(successes(batch)))

    assert [value.result.key for value in delivered] == [value.key for value in batch.items]


@pytest.mark.parametrize("kind", ["duplicate", "missing", "unexpected"])
def test_invalid_response_is_rejected_atomically_and_can_be_failed_explicitly(clock, kind):
    sched = scheduler(clock, max_batch_size=2)
    sched.offer(item("a"))
    sched.offer(item("b"))
    batch = sched.next_batch()
    valid = successes(batch)
    invalid = {"duplicate": (valid[0], valid[0]), "missing": (valid[0],),
               "unexpected": (valid[0], BatchItemResult(item("c").key, 640, 480))}[kind]

    with pytest.raises(ValueError):
        sched.complete(batch, invalid)

    assert sched.snapshot().awaiting_delivery == 0
    failed = sched.fail_batch(batch, "invalid backend response")
    assert len(failed) == 2
    assert all(value.result.failure.code == FailureCode.BATCH for value in failed)
    for value in failed:
        sched.acknowledge(value.item.key)
    assert sched.snapshot().in_flight == 0


def test_partial_failure_does_not_modify_other_results(clock):
    sched = scheduler(clock, max_batch_size=2)
    for camera in ("a", "b"):
        sched.offer(item(camera))
    batch = sched.next_batch()
    results = (BatchItemResult(batch.items[0].key, failure=ItemFailure(FailureCode.ITEM, "bad")),
               BatchItemResult(batch.items[1].key, 640, 480))

    delivered = sched.complete(batch, results)

    assert delivered[0].result.failure.code == FailureCode.ITEM
    assert delivered[1].result.failure is None


def test_rejects_wrong_batch_and_duplicate_completion_or_acknowledgement(clock):
    sched = scheduler(clock, max_batch_size=1)
    sched.offer(item())
    batch = sched.next_batch()
    with pytest.raises(ValueError, match="active"):
        sched.complete(replace(batch), successes(batch))
    with pytest.raises(ValueError, match="acknowledgement"):
        sched.acknowledge(batch.items[0].key)
    delivered = sched.complete(batch, successes(batch))
    with pytest.raises(ValueError, match="active"):
        sched.complete(batch, successes(batch))
    with pytest.raises(ValueError, match="acknowledgement"):
        sched.acknowledge(item(index=1).key)
    sched.acknowledge(delivered[0].item.key)
    with pytest.raises(ValueError, match="acknowledgement"):
        sched.acknowledge(delivered[0].item.key)


def test_stop_cancels_candidates_and_allows_inflight_drain(clock):
    sched = scheduler(clock, max_batch_size=1)
    sched.offer(item())
    batch = sched.next_batch()
    sched.offer(item("b"))

    cancelled = sched.stop()

    assert [value.item.key.camera_id for value in cancelled] == ["b"]
    assert cancelled[0].result.failure.code == FailureCode.CANCELLED
    assert sched.stop() == ()
    assert sched.offer(item(index=1)).reason == "stopping"
    assert sched.next_batch() is None
    with pytest.raises(RuntimeError, match="stopping"):
        sched.start_session("a", "s2")
    deliver(sched, batch)
    clock.now = 5
    assert sched.expire_shutdown() == ()
    assert sched.snapshot().stopping
    assert sched.snapshot().in_flight == 0


@pytest.mark.parametrize("completed", [False, True])
def test_shutdown_reports_unresolved_execution_or_delivery_at_deadline(clock, completed):
    sched = scheduler(clock, max_batch_size=1)
    assert sched.expire_shutdown() == ()
    sched.offer(item())
    batch = sched.next_batch()
    if completed:
        sched.complete(batch, successes(batch))
    sched.stop()
    clock.now = 4.999
    assert sched.expire_shutdown() == ()

    clock.now = 5
    abandoned = sched.expire_shutdown()

    assert len(abandoned) == 1
    assert abandoned[0].result.failure.code == FailureCode.SHUTDOWN_TIMEOUT
    assert sched.snapshot().in_flight == 0
    assert sched.snapshot().awaiting_delivery == 0
    assert sched.expire_shutdown() == ()
    with pytest.raises(ValueError, match="active"):
        sched.complete(batch, successes(batch))


@pytest.mark.parametrize("operation", [
    lambda sched: sched.offer(item("unknown")),
    lambda sched: sched.start_session("unknown", "s1"),
    lambda sched: sched.is_current(item("unknown").key),
    lambda sched: sched.acknowledge(item("unknown").key),
])
def test_rejects_unconfigured_camera_without_growing_state(clock, operation):
    sched = scheduler(clock)
    with pytest.raises(ValueError, match="unknown camera"):
        operation(sched)
    assert sched.snapshot().candidates == sched.snapshot().in_flight == 0


def test_rejects_empty_session_and_future_enqueue_time(clock):
    sched = scheduler(clock)
    with pytest.raises(ValueError, match="session ID"):
        sched.start_session("a", "")
    with pytest.raises(ValueError, match="future"):
        sched.offer(replace(item(), enqueued_at=1))
    assert sched.snapshot().candidates == 0


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf")])
def test_rejects_clock_regression_or_nonfinite_clock(clock, invalid):
    sched = scheduler(clock)
    sched.snapshot()
    clock.now = invalid
    with pytest.raises(ValueError, match="monotonic"):
        sched.next_batch()
