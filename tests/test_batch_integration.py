"""Scheduler correlation through camera-local tracking, real rules and JSONL."""

import json
from dataclasses import replace

import pytest

from ava.batch import BatchDetector, BatchItem, BatchItemResult, BatchKey, CameraTracker
from ava.batch_scheduler import BatchScheduler
from ava.config import ClassFilter, LineCrossingRule, LineGeometry
from ava.detector import Detection, FrameResult
from ava.events import EventRecord
from ava.ingest import Frame
from ava.persistence.jsonl_sink import JsonlEventSink
from ava.rules import RuleEngine

pytestmark = pytest.mark.integration


class FakeBatchDetector:
    def detect_batch(self, batch):
        # Reverse output order deliberately: position is not identity.
        return tuple(BatchItemResult(item.key, 640, 480, (
            Detection(0, "person", 0.9, item.frame.bgr),
        )) for item in reversed(batch.items))


class FakeTracker:
    def __init__(self):
        self.resets = []
        self.observed = []

    def reset(self, session_id):
        self.resets.append(session_id)
        self.observed = []

    def update(self, item, result):
        self.observed.append(item.key)
        return FrameResult(item.key.frame_index, item.frame.timestamp_s,
                           result.width, result.height,
                           [replace(detection, track_id=1) for detection in result.detections])


def test_batches_reach_only_their_camera_rules_and_persist_correlated_events(tmp_path):
    rule = LineCrossingRule(id="door", line=LineGeometry(start=(0, 0.5), end=(1, 0.5)),
                            classes=ClassFilter(include=["person"]), debounce_frames=0)
    sched = BatchScheduler(["a", "b"], max_batch_size=2, clock=lambda: 0)
    detector: BatchDetector = FakeBatchDetector()
    trackers: dict[str, CameraTracker] = {camera: FakeTracker() for camera in ("a", "b")}
    engines = {}
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)

    def reconnect(camera, session):
        sched.start_session(camera, session)
        trackers[camera].reset(session)
        engines[camera] = RuleEngine([rule], camera, session)

    def offer(camera, index, bottom, session="s1"):
        frame = Frame(index, index / 10, (0.4, bottom - 0.1, 0.6, bottom), session_id=session)
        assert sched.offer(BatchItem(BatchKey(camera, session, index), frame, 0)).accepted

    def process(batch):
        for delivery in sched.complete(batch, detector.detect_batch(batch)):
            camera = delivery.item.key.camera_id
            if sched.is_current(delivery.item.key) and delivery.result.failure is None:
                tracked = trackers[camera].update(delivery.item, delivery.result)
                for event in engines[camera].process_frame(tracked, "2026-10-28T12:00:00Z"):
                    sink.write(EventRecord.from_rule_event(event, config_version="1", model_ref=None))
            sched.acknowledge(delivery.item.key)

    try:
        for camera in ("a", "b"):
            reconnect(camera, "s1")
        offer("a", 0, 0.3)
        offer("b", 0, 0.7)
        process(sched.next_batch())
        offer("a", 1, 0.7)
        offer("b", 1, 0.7)
        process(sched.next_batch())
        # Only camera a reconnects while an old-session batch executes.
        offer("a", 2, 0.3)
        offer("b", 2, 0.7)
        old_batch = sched.next_batch()
        reconnect("a", "s2")
        process(old_batch)
        # Fresh session first observation must not cross from old session state.
        offer("a", 0, 0.7, "s2")
        offer("b", 3, 0.3)
        process(sched.next_batch())
    finally:
        sink.close()

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(event["camera_id"], event["session_id"], event["frame_index"])
            for event in events] == [("a", "s1", 1), ("b", "s1", 3)]
    assert all(event["track_id"] == 1 for event in events)
    assert trackers["a"].resets == ["s1", "s2"]
    assert trackers["b"].resets == ["s1"]
    assert [key.frame_index for key in trackers["a"].observed] == [0]
    assert [key.frame_index for key in trackers["b"].observed] == [0, 1, 2, 3]
    assert sched.snapshot().in_flight == 0


def test_slow_or_failed_camera_delivery_leaves_other_cameras_schedulable():
    now = [0.020]
    sched = BatchScheduler(["a", "b"], max_batch_size=2, clock=lambda: now[0])
    for camera in ("a", "b"):
        sched.start_session(camera, "s1")
        frame = Frame(0, 0, (0.1, 0.1, 0.3, 0.3), session_id="s1")
        sched.offer(BatchItem(BatchKey(camera, "s1", 0), frame, 0))
    batch = sched.next_batch()
    deliveries = sched.complete(batch, FakeBatchDetector().detect_batch(batch))
    # A processor has failed or is blocked: its slot remains owned until the
    # composition root resolves it, while b can continue after acknowledgement.
    sched.acknowledge(deliveries[1].item.key)
    for camera in ("a", "b"):
        frame = Frame(1, 0.1, (0.1, 0.1, 0.3, 0.3), session_id="s1")
        sched.offer(BatchItem(BatchKey(camera, "s1", 1), frame, 0))
    assert sched.snapshot().in_flight == 1
    assert sched.snapshot().candidates == 2
    now[0] = 0.041
    other = sched.next_batch()
    assert [item.key.camera_id for item in other.items] == ["b"]
    other_deliveries = sched.complete(other, FakeBatchDetector().detect_batch(other))
    sched.acknowledge(other_deliveries[0].item.key)
    sched.stop()
    sched.acknowledge(deliveries[0].item.key)
    assert sched.snapshot().in_flight == 0
