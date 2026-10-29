import pytest
from test_batch_backend import item

from ava.batch import BatchItemResult
from ava.batch_backend import ByteTrackCameraTracker
from ava.detector import Detection

pytestmark = pytest.mark.integration


def test_real_trackers_keep_independent_camera_state_and_reset():
    trackers = [ByteTrackCameraTracker(10), ByteTrackCameraTracker(10)]
    outputs = []
    for camera, tracker in zip("ab", trackers, strict=True):
        tracker.reset("s")
        submitted = item(camera=camera)
        result = BatchItemResult(submitted.key, 40, 20,
                                 (Detection(0, "person", .9, (.1, .1, .4, .8)),))
        outputs.append(tracker.update(submitted, result))
    assert len(outputs[0].detections) == len(outputs[1].detections) == 1
    assert outputs[0].detections[0].track_id == outputs[1].detections[0].track_id == 1
    trackers[0].reset("next")
    submitted = item(index=1, camera="b")
    result = BatchItemResult(submitted.key, 40, 20,
                             (Detection(0, "person", .9, (.11, .1, .41, .8)),))
    assert trackers[1].update(submitted, result).detections[0].track_id == 1
    with pytest.raises(ValueError, match="order"):
        trackers[1].update(submitted, result)
