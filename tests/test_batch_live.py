import threading
from dataclasses import replace
from queue import Queue
from types import SimpleNamespace

from test_batch_backend import item

from ava.batch import BatchDelivery, BatchItemResult
from ava.batch_live import _Camera
from ava.config import DetectorConfig, LineCrossingRule, LiveCameraConfig, LiveRunConfig
from ava.detector import Detection, FrameResult


def test_queue_overflow_and_cadence_are_separate_terminal_reasons():
    camera = _Camera(LiveCameraConfig(id="a", url="rtsp://example/a", analysis_fps=1), [], 1)
    stop = threading.Event()
    def source(config):
        for index in range(3):
            yield item(index=index).frame
    camera.produce(SimpleNamespace(backend="opencv", reconnect_max=0, reconnect_delay_s=1), stop, source)
    assert camera.decoded == 3
    assert camera.terminal["buffer_full"] == 2
    latest = camera.take()
    assert latest.key.frame_index == 2
    assert camera.eligible(latest)
    assert not camera.eligible(item(index=3))
    assert camera.eligible(item(index=4, session="next"))
    assert camera.take() is None


def test_source_exception_notifies_coordinator():
    camera = _Camera(LiveCameraConfig(id="a", url="rtsp://example/a"), [], 1)
    stop = threading.Event()
    def source(config):
        raise ValueError("decode failed")
    camera.produce(SimpleNamespace(backend="opencv", reconnect_max=0, reconnect_delay_s=1), stop, source)
    assert stop.is_set()
    assert camera.done.is_set()
    assert camera.errors == ["decode failed"]


def test_camera_processor_emits_events_and_acknowledges_after_persistence():
    rule = LineCrossingRule(id="cross", line={"start": (0., .5), "end": (1., .5)})
    camera = _Camera(LiveCameraConfig(id="a", url="rtsp://example/a"), [rule], 1)
    camera.session = "s"
    stop, failure = threading.Event(), threading.Event()
    ack = Queue()
    records = []
    class Sink:
        def write(self, record):
            records.append(record)
            return True
    class Tracker:
        def __init__(self, fps):
            pass
        def reset(self, session):
            pass
        def update(self, submitted, result):
            bottom = .4 if submitted.key.frame_index == 0 else .6
            return FrameResult(submitted.key.frame_index, submitted.frame.timestamp_s, 40, 20,
                               [Detection(0, "person", .9, (.2, .1, .4, bottom), 1)])
    live = LiveRunConfig(cameras=[camera.config], slow_sink_s=.001)
    thread = threading.Thread(target=camera.process, args=(stop, failure, ack, Sink(),
        threading.Lock(), DetectorConfig(model="unused.pt"), live, Tracker))
    thread.start()
    for index in range(2):
        submitted = item(index=index)
        camera.delivery.put(BatchDelivery(submitted, BatchItemResult(submitted.key, 40, 20)))
        assert ack.get(timeout=2) == submitted.key
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert not failure.is_set()
    assert len(records) == camera.events == 1
    assert records[0].camera_id == "a"
    assert camera.terminal["delivered"] == 2


def test_measurement_cohort_includes_cutoff_cancellations():
    camera = _Camera(LiveCameraConfig(id="a", url="rtsp://example/a"), [], 1)
    camera.window = (1., 2.)
    admitted = replace(item(), enqueued_at=1.5)
    outside = replace(item(index=1), enqueued_at=2.)
    camera.finish(admitted, "cancelled")
    camera.finish(outside, "cancelled")
    assert camera.measured["cancelled"] == 1
    assert camera.terminal["cancelled"] == 2


def test_inference_histogram_uses_model_call_duration_instead_of_frame_age():
    recorded = []
    camera = _Camera(LiveCameraConfig(id="a", url="rtsp://example/a"), [], 1)
    camera.telemetry = SimpleNamespace(record_inference=lambda camera_id, seconds: recorded.append((camera_id, seconds)))
    camera.inference_duration_s = .012
    camera.finish(item(), "delivered")
    assert recorded == [("a", .012)]
