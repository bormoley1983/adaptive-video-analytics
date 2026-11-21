from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ava.batch import BatchItem, BatchItemResult, BatchKey, DetectionBatch
from ava.batch_backend import ByteTrackCameraTracker, UltralyticsBatchDetector
from ava.batch_precision import configure_precision, precision_settings, restore_precision
from ava.config import DetectorConfig
from ava.detector import Detection
from ava.ingest import Frame


def item(index=0, session="s", camera="a"):
    return BatchItem(BatchKey(camera, session, index),
                     Frame(index, index / 10, np.zeros((20, 40, 3), dtype=np.uint8),
                           session_id=session), 0.)


def torch_stub():
    return SimpleNamespace(backends=SimpleNamespace(
        cuda=SimpleNamespace(matmul=SimpleNamespace(fp32_precision="none")),
        cudnn=SimpleNamespace(fp32_precision="tf32", conv=SimpleNamespace(fp32_precision="none"))))


def test_precision_policy_restores_previous_values():
    torch = torch_stub()
    before = precision_settings(torch)
    saved = configure_precision(torch)
    assert saved == before
    assert set(precision_settings(torch).values()) == {"ieee"}
    restore_precision(torch, saved)
    assert precision_settings(torch) == before


def test_batch_backend_normalizes_and_clips_without_tracking():
    class Boxes:
        data = None
        def cpu(self):
            return self
        def tolist(self):
            return [[-1, 2, 50, 18, .9, 0]]
    boxes = Boxes()
    boxes.data = boxes
    calls = []
    class Model:
        def predict(self, frames, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(boxes=boxes, names={0: "person"}) for _ in frames]
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), None)
    backend.model = Model()
    submitted = item()
    results = backend.detect_batch(DetectionBatch((submitted,)))
    assert results[0].key == submitted.key
    assert results[0].detections == (Detection(0, "person", .9, (0., .1, 1., .9)),)
    assert calls[0]["rect"] is False
    assert calls[0]["imgsz"] == 640


def test_backend_rejects_unloaded_and_mismatched_counts():
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), None)
    with pytest.raises(RuntimeError, match="not loaded"):
        backend.detect_batch(DetectionBatch((item(),)))
    backend.model = SimpleNamespace(predict=lambda *a, **k: [])
    with pytest.raises(ValueError, match="count"):
        backend.detect_batch(DetectionBatch((item(),)))


def test_backend_checks_precision_before_and_after_prediction():
    torch = torch_stub()
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")))
    backend._torch = torch
    before = configure_precision(torch)
    backend.arithmetic["before"] = before
    def predict(*a, **k):
        torch.backends.cuda.matmul.fp32_precision = "tf32"
        return []
    backend.model = SimpleNamespace(predict=predict)
    with pytest.raises(RuntimeError, match="arithmetic"):
        backend._predict([])
    backend.close()
    assert backend.arithmetic["restored"] == before


def test_tracker_rejects_wrong_session_and_failed_correlation():
    tracker = ByteTrackCameraTracker()
    submitted = item()
    with pytest.raises(ValueError, match="session"):
        tracker.update(submitted, BatchItemResult(submitted.key, 40, 20))
    with pytest.raises(ValueError, match="correlated"):
        tracker.update(submitted, BatchItemResult(BatchKey("b", "s", 0), 40, 20))
    with pytest.raises(ValueError, match="identity"):
        tracker.reset("")


def test_load_warms_once_and_restores_precision_on_close(monkeypatch):
    import sys
    torch = torch_stub()
    calls = []
    class Model:
        def __init__(self, path):
            calls.append(("load", path))
        def to(self, device):
            calls.append(("device", device))
        def predict(self, frames, **kwargs):
            calls.append(("warmup", frames[0].shape))
            return []
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=Model))
    before = precision_settings(torch)
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")))
    backend.load()
    backend.load()
    assert calls == [("load", "unused.pt"), ("device", "cuda"), ("warmup", (640, 640, 3))]
    backend.close()
    assert precision_settings(torch) == before
    assert backend.model is None


def test_predictor_stage_hooks_preserve_results_failures_and_restore_methods():
    from ava.stage_metrics import StageRecorder

    calls = []
    def preprocess(value):
        calls.append(value)
        return value
    def fail(value):
        raise ValueError("prediction failed")
    predictor = SimpleNamespace(preprocess=preprocess, inference=fail, postprocess=preprocess)
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), None,
                                      recorder=StageRecorder())
    backend.model = SimpleNamespace(predictor=predictor)
    backend._attach_stage_hooks()
    assert predictor.preprocess("frame") == "frame"
    with pytest.raises(ValueError, match="prediction failed"):
        predictor.inference("frame")
    stages = {s["stage"]: s for s in backend.recorder.report()["series"]}
    assert stages["predictor_inference_host_wall"]["count"] == 1
    assert calls == ["frame"]
    backend.close()
    assert predictor.preprocess is preprocess
    assert predictor.inference is fail
    assert backend.model is None


@pytest.mark.parametrize("predictor", [None, SimpleNamespace(preprocess=lambda: None)])
def test_stage_hooks_fail_explicitly_when_predictor_boundary_is_unsupported(predictor):
    from ava.stage_metrics import StageRecorder

    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), None,
                                      recorder=StageRecorder())
    backend.model = SimpleNamespace(predictor=predictor)
    with pytest.raises(RuntimeError, match="predictor"):
        backend._attach_stage_hooks()
    backend.close()


# --- Device profiling tests ---

def _cuda_torch_stub(events_recorded=None, sync_calls=None):
    """Build a torch stub whose cuda.Event/cuda.stream/cuda.Stream work for unit tests.

    ``FakeStreamCtx.in_context`` is a class-level flag that is True while any
    instance of the context manager is active — used by tests to verify that
    prediction runs inside the declared stream.
    """
    class FakeEvent:
        def __init__(self, enable_timing=False):
            self.enable_timing = enable_timing
            self._record_count = 0
        def record(self):
            self._record_count += 1
            if events_recorded is not None:
                events_recorded.append(self)
        def query(self):
            return True
        def elapsed_time(self, other):
            return 1.5  # ms

    class FakeStream:
        def __init__(self, device=None):
            self.device = device
            self.sync_count = 0
        def synchronize(self):
            self.sync_count += 1
            if sync_calls is not None:
                sync_calls.append(1)
        def __str__(self):
            return "FakeStream"

    class FakeStreamCtx:
        in_context = False
        active_stream = None  # identity of the stream while context is active
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            type(self).in_context = True
            type(self).active_stream = self.stream
            return self.stream
        def __exit__(self, *a):
            type(self).in_context = False
            type(self).active_stream = None
            return False

    cuda_ns = SimpleNamespace(
        Event=FakeEvent,
        Stream=lambda device=None: FakeStream(device),
        stream=lambda s: FakeStreamCtx(s),
    )
    # Attach the context class to the stub so tests can inspect in_context.
    cuda_ns._FakeStreamCtx = FakeStreamCtx
    return SimpleNamespace(cuda=cuda_ns, backends=SimpleNamespace(
        cuda=SimpleNamespace(matmul=SimpleNamespace(fp32_precision="none")),
        cudnn=SimpleNamespace(fp32_precision="tf32", conv=SimpleNamespace(fp32_precision="none"))))


def _device_backend(recorder=None, sample_every=16):
    from ava.stage_metrics import StageRecorder
    if recorder is None:
        recorder = StageRecorder(sample_every=sample_every, device_profile=True)
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), "cuda",
                                       recorder=recorder, profile_device=True)
    torch_stub = _cuda_torch_stub()
    # Set precision to ieee so _check_precision passes
    torch_stub.backends.cuda.matmul.fp32_precision = "ieee"
    torch_stub.backends.cudnn.fp32_precision = "ieee"
    torch_stub.backends.cudnn.conv.fp32_precision = "ieee"
    backend._torch = torch_stub
    backend._device_stream = SimpleNamespace(synchronize=lambda: None, __str__=lambda s: "test-stream")
    return backend, recorder


def test_device_profile_selection_every_nth_batch():
    from ava.stage_metrics import StageRecorder
    rec = StageRecorder(sample_every=4, device_profile=True)
    results = [rec.select_device_batch() for _ in range(12)]
    assert results == [1, None, None, None, 5, None, None, None, 9, None, None, None]


def test_device_profile_disabled_returns_none():
    from ava.stage_metrics import StageRecorder
    rec = StageRecorder(sample_every=1, device_profile=False)
    assert rec.select_device_batch() is None
    assert rec._device_calls == 0


def test_device_profile_reports_selected_batch_ordinals():
    backend, recorder = _device_backend(sample_every=16)
    backend.model = SimpleNamespace(predict=lambda frames, **kwargs: [])
    for _ in range(33):
        backend._predict([])
    samples = recorder.report()["device_profile"]["samples"]
    assert [sample["ordinal"] for sample in samples] == [1, 17, 33]
    assert [sample["batch_id"] for sample in samples] == ["batch-1", "batch-17", "batch-33"]


def test_device_profile_incomplete_events_are_unavailable():
    backend, recorder = _device_backend(sample_every=1)
    event_class = backend._torch.cuda.Event

    class IncompleteEvent(event_class):
        def query(self):
            return False

    backend._torch.cuda.Event = IncompleteEvent
    backend.model = SimpleNamespace(predict=lambda frames, **kwargs: ["result"])
    assert backend._predict([]) == ["result"]
    section = recorder.report()["device_profile"]
    assert "event read failed" in section["samples"][0]["status"]
    assert section["inference_device_interval_s"] is None


def test_device_profile_prediction_runs_inside_declared_stream():
    """The prediction call must execute while the declared stream context is active."""
    backend, _recorder = _device_backend(sample_every=1)
    ctx_class = backend._torch.cuda._FakeStreamCtx
    in_context_during_predict = []
    active_stream_during_predict = []
    declared_stream = backend._device_stream
    class Model:
        def predict(self, frames, **kwargs):
            in_context_during_predict.append(ctx_class.in_context)
            active_stream_during_predict.append(ctx_class.active_stream)
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)
    backend.model = Model()
    backend.detect_batch(DetectionBatch((item(camera="a"),)))
    assert in_context_during_predict == [True], \
        "prediction must run inside the declared stream context"
    assert active_stream_during_predict == [declared_stream], \
        "prediction must run on the declared stream, not an arbitrary one"


def test_device_profile_records_selected_batch_with_camera_keys():
    backend, recorder = _device_backend(sample_every=1)
    class Model:
        def predict(self, frames, **kwargs):
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=lambda: [[0, 0, 10, 10, .9, 0]]))), names={0: "p"})] * len(frames)
    backend.model = Model()
    batch = DetectionBatch((item(camera="cam1"), item(index=1, camera="cam2")))
    results = backend.detect_batch(batch)
    assert len(results) == 2
    report = recorder.report()
    dp = report["device_profile"]
    assert dp["enabled"] is True
    assert dp["batches_selected"] == 1
    sample = dp["samples"][0]
    assert sample["camera_keys"] == ["cam1", "cam2"]
    assert sample["size"] == 2
    assert sample["status"] == "available"
    assert sample["h2d_interval_s"] is not None
    assert sample["inference_interval_s"] is not None
    assert sample["sync_host_wall_s"] is not None


def test_device_profile_unselected_batch_zero_event_ops_and_syncs():
    """Unselected batches must perform zero event operations and zero synchronizations."""
    events_recorded = []
    sync_calls = []
    from ava.stage_metrics import StageRecorder
    recorder = StageRecorder(sample_every=16, device_profile=True)
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), "cuda",
                                       recorder=recorder, profile_device=True)
    torch_stub = _cuda_torch_stub(events_recorded=events_recorded, sync_calls=sync_calls)
    torch_stub.backends.cuda.matmul.fp32_precision = "ieee"
    torch_stub.backends.cudnn.fp32_precision = "ieee"
    torch_stub.backends.cudnn.conv.fp32_precision = "ieee"
    backend._torch = torch_stub
    stream_obj = torch_stub.cuda.Stream(device="cuda")
    backend._device_stream = stream_obj

    class Model:
        def predict(self, frames, **kwargs):
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)
    backend.model = Model()
    # Batch 0 (ordinal 0) is selected; batch 1 (ordinal 1) is not.
    backend.detect_batch(DetectionBatch((item(camera="a"),)))
    events_after_selected = len(events_recorded)
    syncs_after_selected = stream_obj.sync_count
    assert events_after_selected > 0, "selected batch must record events"
    assert syncs_after_selected == 1, "selected batch must synchronize exactly once"

    backend.detect_batch(DetectionBatch((item(index=1, camera="b"),)))
    # No new events or syncs from the unselected batch.
    assert len(events_recorded) == events_after_selected, \
        "unselected batch must perform zero event operations"
    assert stream_obj.sync_count == syncs_after_selected, \
        "unselected batch must perform zero synchronizations"


def test_device_prediction_failure_no_timing_values_in_series():
    """A failed prediction must not contribute timing values to the report series."""
    backend, recorder = _device_backend(sample_every=1)
    class Model:
        def predict(self, frames, **kwargs):
            raise RuntimeError("GPU OOM")
    backend.model = Model()
    with pytest.raises(RuntimeError, match="GPU OOM"):
        backend.detect_batch(DetectionBatch((item(camera="a"),)))
    dp = recorder.report()["device_profile"]
    sample = dp["samples"][0]
    assert "prediction failed" in sample["status"]
    # Timing values must be None for a failed prediction.
    assert sample["h2d_interval_s"] is None
    assert sample["inference_interval_s"] is None
    assert sample["sync_host_wall_s"] is None
    # The series must not include this sample's (non-existent) values.
    assert dp["h2d_device_interval_s"] is None
    assert dp["inference_device_interval_s"] is None


def test_device_profile_event_setup_failure_does_not_block_prediction():
    """When event creation fails, prediction still runs once and the sample is
    recorded as unavailable with no timing values. The batch is processed —
    measurement failure must not skip detection."""
    backend, recorder = _device_backend(sample_every=1)
    def fail_event(enable_timing=False):
        raise RuntimeError("CUDA init failed")
    backend._torch.cuda.Event = fail_event
    predict_calls = []
    class Model:
        def predict(self, frames, **kwargs):
            predict_calls.append(1)
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)
    backend.model = Model()
    results = backend.detect_batch(DetectionBatch((item(camera="a"),)))
    # Prediction ran exactly once despite the event setup failure.
    assert predict_calls == [1]
    assert len(results) == 1
    dp = recorder.report()["device_profile"]
    sample = dp["samples"][0]
    assert "event setup failed" in sample["status"]
    assert sample["h2d_interval_s"] is None
    assert sample["inference_interval_s"] is None
    assert sample["sync_host_wall_s"] is None


def test_device_profile_end_event_failure_is_measurement_not_prediction():
    """When end-event recording fails after a successful prediction, the result
    is still returned and the sample is recorded as unavailable (measurement
    error), NOT as a prediction failure."""
    backend, recorder = _device_backend(sample_every=1)
    # Make only end-event record() calls fail. Events are created in order:
    # 0=h2d_start, 1=h2d_end, 2=inference_start, 3=inference_end. The start
    # records (events 0 and 2) succeed; the end records (events 1 and 3) raise.
    original_event_cls = backend._torch.cuda.Event
    counter = [0]
    class FailingEndEvent(original_event_cls):
        def __init__(self, enable_timing=False):
            super().__init__(enable_timing)
            self._idx = counter[0]
            counter[0] += 1
        def record(self):
            # End events are at odd indices (h2d_end=1, inference_end=3).
            if self._idx % 2 == 1:
                raise RuntimeError("CUDA stream destroyed")
            super().record()
    backend._torch.cuda.Event = FailingEndEvent
    predict_calls = []
    class Model:
        def predict(self, frames, **kwargs):
            predict_calls.append(1)
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)
    backend.model = Model()
    # Prediction succeeds and its result is returned — no exception.
    results = backend.detect_batch(DetectionBatch((item(camera="a"),)))
    assert predict_calls == [1]
    assert len(results) == 1
    dp = recorder.report()["device_profile"]
    sample = dp["samples"][0]
    # Status reflects the measurement failure, not a prediction failure.
    assert "end event failed" in sample["status"]
    assert "prediction failed" not in sample["status"]
    # No timing values (end events were never recorded).
    assert sample["h2d_interval_s"] is None
    assert sample["inference_interval_s"] is None


@pytest.mark.parametrize("failure_point", ["entry", "exit"])
def test_device_stream_context_failure_preserves_prediction(failure_point):
    backend, recorder = _device_backend(sample_every=1)
    original_stream = backend._torch.cuda.stream
    context_count = 0

    def stream_with_failure(stream):
        nonlocal context_count
        context_count += 1
        context = original_stream(stream)
        if context_count == 1:
            return context

        class FailingContext:
            def __enter__(self):
                if failure_point == "entry":
                    raise RuntimeError("stream entry failed")
                return context.__enter__()

            def __exit__(self, *args):
                context.__exit__(*args)
                raise RuntimeError("stream exit failed")

        return FailingContext()

    backend._torch.cuda.stream = stream_with_failure
    calls = []

    class Model:
        def predict(self, frames, **kwargs):
            calls.append(1)
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)

    backend.model = Model()
    results = backend.detect_batch(DetectionBatch((item(camera="a"),)))
    sample = recorder.report()["device_profile"]["samples"][0]
    assert calls == [1]
    assert len(results) == 1
    assert "stream context failed" in sample["status"]
    assert sample["inference_interval_s"] is None


def test_device_setup_and_prediction_failure_records_original_error():
    backend, recorder = _device_backend(sample_every=1)

    def fail_event(enable_timing=False):
        raise RuntimeError("event setup failed")

    class Model:
        def predict(self, frames, **kwargs):
            raise ValueError("prediction failed")

    backend._torch.cuda.Event = fail_event
    backend.model = Model()
    with pytest.raises(ValueError, match="prediction failed"):
        backend.detect_batch(DetectionBatch((item(camera="a"),)))
    sample = recorder.report()["device_profile"]["samples"][0]
    assert "prediction failed: ValueError" in sample["status"]
    assert sample["inference_interval_s"] is None


def test_device_profile_window_filter_uses_host_timestamps():
    backend, recorder = _device_backend(sample_every=1)
    class Model:
        def predict(self, frames, **kwargs):
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)
    backend.model = Model()
    import time
    now = time.monotonic()
    recorder.window = (now + 100, now + 200)  # window in the future; current batch won't match
    backend.detect_batch(DetectionBatch((item(camera="a"),)))
    dp = recorder.report()["device_profile"]
    assert dp["batches_selected"] == 0


def test_device_profile_report_interval_scope_disclosure():
    backend, recorder = _device_backend(sample_every=1)
    class Model:
        def predict(self, frames, **kwargs):
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)
    backend.model = Model()
    backend.detect_batch(DetectionBatch((item(camera="a"),)))
    dp = recorder.report()["device_profile"]
    scope = dp["interval_scope"]
    assert "same whole prediction call" in scope
    assert "not separately attributable" in scope


def test_device_profile_disabled_path_byte_identical():
    """With profile_device=False, _predict takes the plain path with no event ops."""
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), None)
    calls = []
    class Model:
        def predict(self, frames, **kwargs):
            calls.append(1)
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=list))), names={})] * len(frames)
    backend.model = Model()
    backend._predict([np.zeros((20, 40, 3), dtype=np.uint8)])
    assert calls == [1]
    assert backend.profile_device is False


def test_device_profile_requires_cuda_device():
    """load() must raise when profile_device=True but device is not CUDA."""
    import sys

    from ava.stage_metrics import StageRecorder

    torch_stub = _cuda_torch_stub()
    predictor = SimpleNamespace(preprocess=lambda v: v, inference=lambda v: v, postprocess=lambda v: v)
    class Model:
        def __init__(self, path):
            self.predictor = predictor
        def to(self, device):
            pass
        def predict(self, frames, **kwargs):
            return []
    from unittest import mock
    with mock.patch.dict(sys.modules, {"torch": torch_stub,
                                       "ultralytics": SimpleNamespace(YOLO=Model)}):
        recorder = StageRecorder(device_profile=True)
        backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), None,
                                           recorder=recorder, profile_device=True)
        assert backend.device == "cpu"
        with pytest.raises(RuntimeError, match="CUDA device"):
            backend.load()


def test_device_profile_stage_hooks_include_inference_when_enabled():
    from ava.stage_metrics import StageRecorder
    recorder = StageRecorder(device_profile=True)
    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), "cuda",
                                       recorder=recorder, profile_device=True)
    calls = []
    def preprocess(v):
        calls.append(("preprocess", v))
        return v
    def inference(v):
        calls.append(("inference", v))
        return v
    def postprocess(v):
        calls.append(("postprocess", v))
        return v
    predictor = SimpleNamespace(preprocess=preprocess, inference=inference, postprocess=postprocess)
    backend.model = SimpleNamespace(predictor=predictor)
    backend._attach_stage_hooks()
    # Call all three
    predictor.preprocess("f")
    predictor.inference("f")
    predictor.postprocess("f")
    stages = {s["stage"] for s in recorder.report()["series"]}
    assert "predictor_preprocess_host_wall" in stages
    assert "predictor_postprocess_host_wall" in stages
    assert "predictor_inference_host_wall" in stages


def test_device_profile_config_requires_stage_profile():
    from ava.config import BatchConfig
    with pytest.raises(ValueError, match="profile_stages"):
        BatchConfig(profile_device=True, profile_stages=False)
    # Valid: both enabled
    cfg = BatchConfig(profile_device=True, profile_stages=True)
    assert cfg.profile_device is True


def test_device_profile_config_default_disabled():
    from ava.config import BatchConfig
    cfg = BatchConfig()
    assert cfg.profile_device is False


def test_warmup_does_not_consume_selection_ordinal():
    """Warm-up in load() must not participate in device batch selection."""
    import sys
    from unittest import mock

    events_recorded = []
    sync_calls = []
    torch_stub = _cuda_torch_stub(events_recorded=events_recorded, sync_calls=sync_calls)
    torch_stub.backends.cuda.matmul.fp32_precision = "ieee"
    torch_stub.backends.cudnn.fp32_precision = "ieee"
    torch_stub.backends.cudnn.conv.fp32_precision = "ieee"

    warmup_calls = []
    predictor = SimpleNamespace(preprocess=lambda v: v, inference=lambda v: v, postprocess=lambda v: v)
    class Model:
        def __init__(self, path):
            self.predictor = predictor
        def to(self, device):
            pass
        def predict(self, frames, **kwargs):
            warmup_calls.append(len(frames))
            return []
    from ava.stage_metrics import StageRecorder
    recorder = StageRecorder(sample_every=16, device_profile=True)
    with mock.patch.dict(sys.modules, {"torch": torch_stub,
                                       "ultralytics": SimpleNamespace(YOLO=Model)}):
        backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), "cuda",
                                           recorder=recorder, profile_device=True)
        backend.load()
    # Warm-up ran once.
    assert warmup_calls == [1]
    # No device events were recorded during warm-up (stream didn't exist yet).
    assert len(events_recorded) == 0, "warm-up must not create CUDA events"
    # Selection counter was not consumed by warm-up.
    assert recorder._device_calls == 0, "warm-up must not consume a selection ordinal"
