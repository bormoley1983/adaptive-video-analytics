"""Detector adapter contract checks (no GPU, no model download).

Covers the pure device-resolution logic and the result-parsing path of both
backends by injecting a fake model object. Real YOLO inference is a hardware
path excluded from the unit gate (see the benchmark report's narrow-exception
note); here we validate the owned boundary: that raw ultralytics-style results
are mapped to normalised Detection records correctly.
"""

from __future__ import annotations

import numpy as np
import pytest

from ava.detector import (
    ConventionalDetector,
    Detection,
    FrameResult,
    PromptedDetector,
    _resolve_device,
)

# --- device resolution -------------------------------------------------------


def test_resolve_device_none_is_cpu() -> None:
    assert _resolve_device(None) == "cpu"


def test_resolve_device_index_is_cuda() -> None:
    assert _resolve_device("0") == "cuda"


def test_resolve_device_uuid_is_cuda() -> None:
    assert _resolve_device("GPU-facd1ddc-c4ab-7ac9-0000-ead909e0ec42") == "cuda"


# --- dataclasses -------------------------------------------------------------


def test_detection_defaults_track_id_none() -> None:
    d = Detection(class_id=0, label="person", confidence=0.9,
                  box=(0.1, 0.2, 0.3, 0.5))
    assert d.track_id is None


def test_frame_result_defaults_empty_detections() -> None:
    fr = FrameResult(index=0, timestamp_s=0.0, width=640, height=480)
    assert fr.detections == []


# --- fake ultralytics result -------------------------------------------------


class _Row:
    """Mimics an ultralytics tensor row: indexable and has tolist()."""

    def __init__(self, values):
        self._values = list(map(float, values))

    def __getitem__(self, i):
        return self._values[i]

    def tolist(self):
        return list(self._values)


class _FakeBoxes:
    def __init__(self, xyxy, cls, conf, ids=None):
        self.xyxy = [_Row(b) for b in xyxy]
        self.cls = list(cls)
        self.conf = list(conf)
        self.id = ids

    def __len__(self):
        return len(self.xyxy)


class _FakeResult:
    def __init__(self, boxes, names):
        self.boxes = boxes
        self.names = names


def _fake_model(boxes, names, track=False):
    """A stand-in for an ultralytics YOLO model returning canned results."""

    class _Model:
        def to(self, device):
            return self

        def track(self, frame, **kw):
            return [_FakeResult(boxes, names)]

        def predict(self, frame, **kw):
            return [_FakeResult(boxes, names)]

        def set_classes(self, prompts):
            self.prompts = list(prompts)

    m = _Model()
    return m


# --- conventional detector: result parsing -----------------------------------


def test_conventional_detect_maps_boxes_to_normalised_detections(tmp_path) -> None:
    det = ConventionalDetector(tmp_path / "yolo26n.pt", device=None,
                               confidence=0.35, iou=0.5)
    # One box in a 100x200 frame (h=100, w=200): x in [0,100], y in [0,200].
    boxes = _FakeBoxes(xyxy=[(10, 20, 50, 100)], cls=[0], conf=[0.9], ids=[7])
    det._model = _fake_model(boxes, {0: "person"})

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(3, 0.3, frame)

    assert isinstance(result, FrameResult)
    assert result.index == 3
    assert result.timestamp_s == pytest.approx(0.3)
    assert (result.width, result.height) == (200, 100)
    assert len(result.detections) == 1
    d = result.detections[0]
    assert d.class_id == 0
    assert d.label == "person"
    assert d.confidence == pytest.approx(0.9)
    # Normalised: x / w, y / h.
    assert d.box == pytest.approx((10 / 200, 20 / 100, 50 / 200, 100 / 100))
    assert d.track_id == 7


def test_conventional_detect_no_boxes_returns_empty(tmp_path) -> None:
    det = ConventionalDetector(tmp_path / "yolo26n.pt", device=None)
    boxes = _FakeBoxes(xyxy=[], cls=[], conf=[])
    det._model = _fake_model(boxes, {0: "person"})

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(0, 0.0, frame)
    assert result.detections == []


def test_conventional_detect_unknown_class_uses_id_label(tmp_path) -> None:
    det = ConventionalDetector(tmp_path / "yolo26n.pt", device=None)
    boxes = _FakeBoxes(xyxy=[(10, 20, 50, 100)], cls=[9], conf=[0.5])
    det._model = _fake_model(boxes, {0: "person"})  # id 9 not in names

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(0, 0.0, frame)
    assert result.detections[0].label == "9"


def test_conventional_detect_without_track_ids(tmp_path) -> None:
    det = ConventionalDetector(tmp_path / "yolo26n.pt", device=None)
    boxes = _FakeBoxes(xyxy=[(10, 20, 50, 100)], cls=[0], conf=[0.9], ids=None)
    det._model = _fake_model(boxes, {0: "person"})

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(0, 0.0, frame)
    assert result.detections[0].track_id is None


def test_conventional_detect_requires_load(tmp_path) -> None:
    det = ConventionalDetector(tmp_path / "yolo26n.pt", device=None)
    with pytest.raises(AssertionError):
        det.detect(0, 0.0, np.zeros((10, 10, 3), dtype=np.uint8))


# --- prompted detector: result parsing ---------------------------------------


def test_prompted_detect_maps_boxes_to_detections(tmp_path) -> None:
    det = PromptedDetector(tmp_path / "yoloe.pt", device=None, prompts=["person"])
    boxes = _FakeBoxes(xyxy=[(10, 20, 50, 100)], cls=[0], conf=[0.8])
    det._model = _fake_model(boxes, {0: "person"})

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(1, 0.1, frame)

    assert len(result.detections) == 1
    d = result.detections[0]
    assert d.label == "person"
    assert d.confidence == pytest.approx(0.8)
    assert d.box == pytest.approx((10 / 200, 20 / 100, 50 / 200, 100 / 100))
    # Prompted path does not assign track ids.
    assert d.track_id is None


def test_prompted_detect_no_boxes_returns_empty(tmp_path) -> None:
    det = PromptedDetector(tmp_path / "yoloe.pt", device=None, prompts=["car"])
    boxes = _FakeBoxes(xyxy=[], cls=[], conf=[])
    det._model = _fake_model(boxes, {0: "car"})

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(0, 0.0, frame)
    assert result.detections == []


def test_prompted_detect_requires_load(tmp_path) -> None:
    det = PromptedDetector(tmp_path / "yoloe.pt", device=None)
    with pytest.raises(AssertionError):
        det.detect(0, 0.0, np.zeros((10, 10, 3), dtype=np.uint8))


# --- loaded-model guard: the assert passes when a model is present -----------


def test_conventional_detect_loaded_model_passes_guard(tmp_path) -> None:
    # A loaded (fake) model satisfies the load-required guard; detect proceeds.
    det = ConventionalDetector(tmp_path / "yolo26n.pt", device=None)
    boxes = _FakeBoxes(xyxy=[(10, 20, 50, 100)], cls=[0], conf=[0.9], ids=[3])
    det._model = _fake_model(boxes, {0: "person"})

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(0, 0.0, frame)
    assert len(result.detections) == 1


def test_prompted_detect_loaded_model_passes_guard(tmp_path) -> None:
    # A loaded (fake) model satisfies the load-required guard; detect proceeds.
    det = PromptedDetector(tmp_path / "yoloe.pt", device=None, prompts=["person"])
    boxes = _FakeBoxes(xyxy=[(10, 20, 50, 100)], cls=[0], conf=[0.8])
    det._model = _fake_model(boxes, {0: "person"})

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    result = det.detect(0, 0.0, frame)
    assert len(result.detections) == 1
