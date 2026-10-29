from pathlib import Path

import pytest
from pydantic import ValidationError

from ava.batch_backend import validate_backend
from ava.config import BatchConfig, DetectorConfig, LiveRunConfig
from ava.live_runner import run_live


@pytest.mark.parametrize("field,value", [("max_batch_size", 0), ("max_batch_size", True),
    ("max_batch_size", 1.5), ("max_wait_ms", 0), ("readiness_timeout_s", -1),
    ("warmup_s", -1), ("shutdown_timeout_s", float("inf")), ("unknown", 1)])
def test_rejects_invalid_batch_controls(field, value):
    with pytest.raises(ValidationError):
        BatchConfig(**{field: value})


def test_batch_defaults_and_large_size():
    assert BatchConfig().max_batch_size == 16
    assert BatchConfig(max_batch_size=100).max_batch_size == 100


def test_rejects_controls_in_compatibility_mode():
    with pytest.raises(ValidationError, match="batch controls require"):
        LiveRunConfig(batch={}, cameras=[{"id": "a", "url": "rtsp://example/a"}])


@pytest.mark.parametrize("backend,kind,error", [("deepstream", "conventional", NotImplementedError),
                                               ("ultralytics", "prompted", ValueError)])
def test_unsupported_backend_fails_before_output_creation(tmp_path, backend, kind, error):
    live = LiveRunConfig(detector_mode="batched", inference_backend=backend,
                         cameras=[{"id": "a", "url": "rtsp://example/a"}])
    detector = DetectorConfig(kind=kind, model=Path("missing.pt"))
    with pytest.raises(error):
        run_live(live, detector, tmp_path / "untouched")
    assert not (tmp_path / "untouched").exists()


def test_accepts_conventional_batch_backend():
    live = LiveRunConfig(detector_mode="batched", cameras=[{"id": "a", "url": "rtsp://example/a"}])
    validate_backend(live, DetectorConfig(model=Path("model.pt")))
