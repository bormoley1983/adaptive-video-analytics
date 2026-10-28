"""Contract validation without models, services or hardware."""

from dataclasses import replace

import pytest

from ava.batch import (
    BatchItem,
    BatchItemResult,
    BatchKey,
    DetectionBatch,
    FailureCode,
    ItemFailure,
)
from ava.detector import Detection
from ava.ingest import Frame


def item(camera="a", session="s1", index=0):
    return BatchItem(BatchKey(camera, session, index),
                     Frame(index, index / 10, object(), session_id=session), 0.0)


@pytest.mark.parametrize("camera,session,index", [
    ("", "s1", 0), ("a", "", 0), ("a", "s1", -1), (1, "s1", 0),
    ("a", 1, 0), ("a", "s1", True), ("a", "s1", 0.5),
])
def test_rejects_incomplete_identity(camera, session, index):
    with pytest.raises(ValueError, match="identity"):
        BatchKey(camera, session, index)


@pytest.mark.parametrize("field,value", [
    ("index", 1), ("session_id", "s2"),
    ("timestamp_s", -1), ("timestamp_s", float("nan")), ("timestamp_s", float("inf")),
])
def test_rejects_frame_metadata_inconsistent_with_identity(field, value):
    original = item()
    with pytest.raises(ValueError):
        replace(original, frame=replace(original.frame, **{field: value}))


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_rejects_nonfinite_enqueue_time(value):
    with pytest.raises(ValueError, match="enqueue"):
        replace(item(), enqueued_at=value)


@pytest.mark.parametrize("items", [(), [], (item(), item(index=1))])
def test_rejects_empty_mutable_or_repeated_camera_batches(items):
    with pytest.raises(ValueError):
        DetectionBatch(items)


def test_batch_preserves_frame_and_capture_metadata():
    frame = Frame(1, 0.1, object(), "2026-10-28T12:00:00Z", "live_epoch", "s1")
    submitted = BatchItem(BatchKey("a", "s1", 1), frame, 2)
    batch = DetectionBatch((submitted, item("b")))

    assert batch.items[0].frame is frame
    assert batch.items[0].frame.capture_utc == "2026-10-28T12:00:00Z"
    assert batch.items[0].frame.time_basis == "live_epoch"


@pytest.mark.parametrize("code,message", [
    ("unknown", "error"), (FailureCode.ITEM, ""), (FailureCode.ITEM, 1),
])
def test_rejects_untyped_or_empty_failure(code, message):
    with pytest.raises(ValueError, match="typed failure"):
        ItemFailure(code, message)


@pytest.mark.parametrize("width,height", [
    (0, 10), (10, 0), (-1, 10), (True, 10), (10, 1.5),
])
def test_success_requires_source_dimensions(width, height):
    with pytest.raises(ValueError, match="dimensions"):
        BatchItemResult(item().key, width, height)


@pytest.mark.parametrize("metadata", [
    {"width": 10}, {"height": 10},
    {"detections": (Detection(0, "person", 0.9, (0, 0, 1, 1)),)},
])
def test_failed_result_rejects_success_metadata(metadata):
    with pytest.raises(ValueError, match="failed result"):
        BatchItemResult(item().key, failure=ItemFailure(FailureCode.ITEM, "bad frame"), **metadata)


def test_rejects_untyped_failure_result():
    with pytest.raises(TypeError, match="typed ItemFailure"):
        BatchItemResult(item().key, failure="error")


@pytest.mark.parametrize("detection", [
    Detection(0, "person", 0.9, (0, 0, 1, 1), 1),
    Detection(0, "person", 0.9, (-0.1, 0, 1, 1)),
    Detection(0, "person", 0.9, (0, float("nan"), 1, 1)),
    Detection(0, "person", 0.9, (0.8, 0, 0.2, 1)),
    Detection(0, "person", 0.9, (0, 0.8, 1, 0.2)),
    Detection(0, "person", 1.1, (0, 0, 1, 1)),
    Detection(0, "person", float("inf"), (0, 0, 1, 1)),
])
def test_detection_contract_rejects_tracker_state_or_invalid_normalized_values(detection):
    with pytest.raises(ValueError):
        BatchItemResult(item().key, 640, 480, (detection,))


def test_rejects_mutable_detection_sequence():
    with pytest.raises(TypeError, match="immutable"):
        BatchItemResult(item().key, 640, 480, [])


def test_empty_and_boundary_detections_are_successful():
    empty = BatchItemResult(item().key, 1, 1)
    full = BatchItemResult(item().key, 640, 480,
                          (Detection(0, "person", 0, (0, 0, 1, 1)),))

    assert empty.failure is None
    assert full.detections[0].track_id is None
