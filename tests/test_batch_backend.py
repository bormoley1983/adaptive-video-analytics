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
