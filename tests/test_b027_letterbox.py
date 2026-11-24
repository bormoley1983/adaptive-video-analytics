"""Letterbox contract tests for the batched Ultralytics preprocessing boundary.

These CPU-only tests verify that the installed Ultralytics preprocessor produces
the documented square-letterbox tensor when called with the exact arguments the
AVA batched backend uses (imgsz=640, rect=False):

  * 1280x720 input -> 640x360 content region + 140 gray-114 rows top/bottom
  * output shape (N, 3, 640, 640), float32, values in [0.0, 1.0]
  * RGB channel order (BGR input is flipped)
  * /255 normalization

The tests exercise the actual ``BasePredictor.preprocess`` method (not a
reimplementation), using a spatially varying oracle to detect stretching and
a same-shape batch to verify multi-frame layout.

Scope limitation: these tests exercise the batched predictor boundary only.
They do NOT claim parity for single-frame conventional or prompted paths
(which do not explicitly pass both imgsz=640 and rect=False), nor for any
DeepStream SDK path (not yet implemented in AVA).
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.fixture()
def predictor(tmp_path):
    """Run real preprocessing in a fresh interpreter with isolated settings."""
    class IsolatedPredictor:
        def preprocess(self, frames):
            source = tmp_path / "frames.npy"
            output = tmp_path / "output.npy"
            config = tmp_path / "config"
            config.mkdir(exist_ok=True)
            np.save(source, np.stack(frames))
            env = os.environ.copy()
            env["YOLO_CONFIG_DIR"] = str(config)
            worker = r"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import numpy as np
from ultralytics.engine.predictor import BasePredictor
from ultralytics.utils import USER_CONFIG_DIR

assert USER_CONFIG_DIR == Path(os.environ["YOLO_CONFIG_DIR"]) / "Ultralytics"
predictor = BasePredictor.__new__(BasePredictor)
predictor.imgsz = 640
predictor.args = SimpleNamespace(rect=False, scale_fill=False)
predictor.device = "cpu"
predictor.model = MagicMock()
predictor.model.fp16 = False
predictor.model.format = "pt"
predictor.model.stride = 32
frames = np.load(sys.argv[1])
np.save(sys.argv[2], predictor.preprocess(list(frames)).numpy())
"""
            subprocess.run(
                [sys.executable, "-c", worker, str(source), str(output)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            return np.load(output)

    return IsolatedPredictor()


def _vertical_gradient_bgr(height: int, width: int) -> np.ndarray:
    """BGR image with a vertical gradient in the R channel (row 0=0, last row~255).

    B and G channels are constant. After letterbox to 640x640 with content
    rows 140..500 (360 rows scaled from `height`), the R channel at content
    row i should be approximately (i * height / 360) / 255, NOT (i / 640) / 255.
    This distinguishes correct aspect-preserving scaling from stretching.
    """
    img = np.zeros((height, width, 3), dtype=np.uint8)
    for r in range(height):
        img[r, :, 2] = int(r * 255 / (height - 1)) if height > 1 else 0
    return img


def test_letterbox_output_shape(predictor):
    """1280x720 BGR input produces (1, 3, 640, 640) float32 tensor."""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    out = predictor.preprocess([img])
    assert tuple(out.shape) == (1, 3, 640, 640), f"expected (1,3,640,640), got {tuple(out.shape)}"
    assert out.dtype == np.float32, f"expected float32, got {out.dtype}"


def test_letterbox_content_geometry_spatial_oracle(predictor):
    """Spatially varying R channel verifies aspect-preserving scaling (no stretch).

    A vertical gradient in the source R channel produces a known linear ramp
    in the content region. The content occupies rows 140..500 (360 rows) of
    the 640x640 output, scaled from the full 720-row source. If the
    preprocessor stretched 720 rows into 640 (instead of scaling to 360 and
    padding), the midpoint value would be ~0.28 instead of ~0.50. This test
    detects that defect.
    """
    img = _vertical_gradient_bgr(720, 1280)
    out = predictor.preprocess([img])

    # R channel (output channel 0 after BGR->RGB flip), center column.
    r_ch = out[0, 0, :, 320]

    # Content region: rows 140..499 (360 rows). The content is the full 720-row
    # source scaled to 360 rows, so content row i maps to source row ~i*2.
    # Content row 0 -> source row 0 -> R=0 -> 0.0
    assert abs(r_ch[140].item()) < 0.01, f"content start should be ~0.0, got {r_ch[140].item()}"

    # Content row 180 (middle of 360-row content) -> source row ~360 -> R~127 -> ~0.498
    # The gradient spans the full 720-row source compressed into 360 content rows,
    # so the midpoint is ~0.50, not 0.70. A stretched-to-640 result would have
    # no uniform padding boundary at row 140 (checked below).
    mid_val = r_ch[140 + 180].item()
    assert abs(mid_val - 0.50) < 0.02, (
        f"content midpoint should be ~0.50 (720-row gradient in 360 content rows), "
        f"got {mid_val:.4f}"
    )

    # Content row 359 (last content row) -> source row ~718 -> R~254 -> ~0.996
    end_val = r_ch[140 + 359].item()
    assert abs(end_val - 1.0) < 0.01, f"content end should be ~1.0, got {end_val:.4f}"

    # Padding regions: rows 0..139 and 500..639 are gray-114 on all channels.
    # The existence of a sharp boundary at row 140 (content starts) and row 500
    # (content ends) is itself the stretch detector: a stretched image would
    # have no uniform padding rows.
    top_pad = out[0, :, :140, :]
    bot_pad = out[0, :, 500:, :]
    expected_pad = 114.0 / 255.0
    assert abs(top_pad.mean().item() - expected_pad) < 1e-6, (
        f"top padding should be ~{expected_pad:.4f}, got {top_pad.mean().item():.4f}"
    )
    assert abs(bot_pad.mean().item() - expected_pad) < 1e-6, (
        f"bottom padding should be ~{expected_pad:.4f}, got {bot_pad.mean().item():.4f}"
    )
    # Padding must be uniform (zero variance) — a stretched image would have
    # varying values in the "padding" region.
    assert top_pad.std().item() < 1e-6, f"top padding should be uniform, std={top_pad.std().item():.6f}"
    assert bot_pad.std().item() < 1e-6, f"bottom padding should be uniform, std={bot_pad.std().item():.6f}"


def test_letterbox_rgb_order(predictor):
    """BGR input channels are flipped to RGB in the output tensor."""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    img[:, :, 0] = 100  # Blue
    img[:, :, 1] = 150  # Green
    img[:, :, 2] = 200  # Red

    out = predictor.preprocess([img])

    # Output channel 0 = R (input channel 2), channel 1 = G, channel 2 = B.
    r_content = out[0, 0, 140:500, :]
    g_content = out[0, 1, 140:500, :]
    b_content = out[0, 2, 140:500, :]

    assert abs(r_content.mean().item() - 200.0 / 255.0) < 1e-6, (
        f"R channel should be ~{200/255:.4f}, got {r_content.mean().item():.4f}"
    )
    assert abs(g_content.mean().item() - 150.0 / 255.0) < 1e-6, (
        f"G channel should be ~{150/255:.4f}, got {g_content.mean().item():.4f}"
    )
    assert abs(b_content.mean().item() - 100.0 / 255.0) < 1e-6, (
        f"B channel should be ~{100/255:.4f}, got {b_content.mean().item():.4f}"
    )


def test_letterbox_normalization_range(predictor):
    """All output values are in [0.0, 1.0] after /255 normalization."""
    rng = np.random.default_rng(42)
    img = rng.integers(0, 256, size=(720, 1280, 3), dtype=np.uint8)
    out = predictor.preprocess([img])
    assert out.min().item() >= 0.0, f"min value {out.min().item():.4f} below 0.0"
    assert out.max().item() <= 1.0, f"max value {out.max().item():.4f} above 1.0"


def test_letterbox_padding_exact_gray_114(predictor):
    """Padding pixels are exactly gray 114/255 (not black, not white)."""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    out = predictor.preprocess([img])

    corner = out[0, :, 0, 0]
    expected = 114.0 / 255.0
    for ch in range(3):
        assert abs(corner[ch].item() - expected) < 1e-6, (
            f"corner channel {ch} = {corner[ch].item():.6f}, expected {expected:.6f}"
        )


def test_letterbox_same_shape_batch(predictor):
    """Two identical 720x1280 frames produce (2, 3, 640, 640) with matching content."""
    img = np.full((720, 1280, 3), 200, dtype=np.uint8)
    out = predictor.preprocess([img, img])

    assert tuple(out.shape) == (2, 3, 640, 640), f"expected (2,3,640,640), got {tuple(out.shape)}"
    # Both frames should have identical content.
    frame_0_content = out[0, 0, 140:500, :]
    frame_1_content = out[1, 0, 140:500, :]
    assert abs(frame_0_content.mean().item() - frame_1_content.mean().item()) < 1e-6
    # Content value should be the input value normalized.
    assert abs(frame_0_content.mean().item() - 200.0 / 255.0) < 1e-6


def test_batch_backend_passes_rect_false_and_imgsz_640():
    """The AVA batched backend passes rect=False and imgsz=640 to the model.

    This pairs with the existing argument-capture tests in this file (which
    already assert these values through a fake model) and anchors the
    letterbox contract to the exact arguments the production path uses.
    """
    from pathlib import Path

    from ava.batch import BatchItem, BatchKey, DetectionBatch
    from ava.batch_backend import UltralyticsBatchDetector
    from ava.config import DetectorConfig
    from ava.ingest import Frame

    calls: list[dict] = []

    class FakeModel:
        def __init__(self):
            self.stride = 32

        def predict(self, *args, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(
                boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda: np.empty((0, 6), dtype=np.float32))),
                names={0: "person"},
            )]

    frame = Frame(index=0, timestamp_s=1.0, bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
                  session_id="s")
    item = BatchItem(key=BatchKey(camera_id="a", session_id="s", frame_index=0),
                     frame=frame, enqueued_at=1.0)
    batch = DetectionBatch(items=(item,))

    backend = UltralyticsBatchDetector(DetectorConfig(model=Path("unused.pt")), "cpu")
    backend.model = FakeModel()
    backend._torch = SimpleNamespace(backends=SimpleNamespace(
        cuda=SimpleNamespace(matmul=SimpleNamespace(fp32_precision="ieee")),
        cudnn=SimpleNamespace(fp32_precision="ieee", conv=SimpleNamespace(fp32_precision="ieee")),
    ))
    backend.detect_batch(batch)

    assert len(calls) == 1
    assert calls[0].get("rect") is False, f"expected rect=False, got {calls[0].get('rect')}"
    assert calls[0].get("imgsz") == 640, f"expected imgsz=640, got {calls[0].get('imgsz')}"
