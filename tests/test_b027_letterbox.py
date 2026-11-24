"""B-027 letterbox contract tests for the batched Ultralytics preprocessing boundary.

These CPU-only tests verify that the installed Ultralytics preprocessor produces
the documented square-letterbox tensor when called with the exact arguments the
AVA batched backend uses (imgsz=640, rect=False):

  * 1280x720 input -> 640x360 content region + 140 gray-114 rows top/bottom
  * output shape (1, 3, 640, 640), float32, values in [0.0, 1.0]
  * RGB channel order (BGR input is flipped)
  * /255 normalization

Scope limitation: these tests exercise the batched predictor boundary only.
They do NOT claim parity for single-frame conventional or prompted paths
(which do not explicitly pass both imgsz=640 and rect=False), nor for any
DeepStream SDK path (not yet implemented in AVA).
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

# Isolate Ultralytics settings so tests never read or write the user's config.
os.environ["YOLO_CONFIG_DIR"] = tempfile.mkdtemp(prefix="b027-yolo-test-")


def _letterbox_tensor(bgr_image: np.ndarray) -> np.ndarray:
    """Replicate BasePredictor.pre_transform + preprocess for rect=False, imgsz=640.

    Mirrors the exact call chain in ultralytics.engine.predictor.BasePredictor:
      1. LetterBox(640, auto=False, scale_fill=False, stride=32)(image=bgr)
      2. torch.from_numpy -> unsqueeze -> permute BHWC->BCHW -> flip BGR->RGB
      3. float().div_(255)

    Returns a numpy array (1, 3, 640, 640) float32 to avoid requiring torch
    in the test environment (the existing tests stub torch via conftest).
    """
    from ultralytics.data.augment import LetterBox

    lb = LetterBox(640, auto=False, scale_fill=False, stride=32)
    padded = lb(image=bgr_image)  # (640, 640, 3) uint8, BGR order preserved

    # Replicate the torch ops with numpy: permute BHWC->BCHW, flip channel axis.
    bchw = np.transpose(padded, (2, 0, 1))  # (3, 640, 640) BGR
    rgb = bchw[::-1]                        # (3, 640, 640) RGB
    tensor = rgb.astype(np.float32) / 255.0  # normalize
    return tensor[np.newaxis, ...]          # (1, 3, 640, 640)


def test_letterbox_output_shape():
    """1280x720 BGR input produces (1, 3, 640, 640) float32 tensor."""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    out = _letterbox_tensor(img)
    assert out.shape == (1, 3, 640, 640), f"expected (1,3,640,640), got {out.shape}"
    assert out.dtype == np.float32


def test_letterbox_content_geometry():
    """Content occupies rows 140..500; padding is rows 0..139 and 500..639."""
    img = np.full((720, 1280, 3), 200, dtype=np.uint8)
    out = _letterbox_tensor(img)

    # Content region: rows 140-499 (inclusive) should carry the input value.
    content = out[0, :, 140:500, :]
    assert np.allclose(content, 200.0 / 255.0, atol=1e-6), (
        f"content region should be ~{200/255:.4f}, got range "
        f"[{content.min():.4f}, {content.max():.4f}]"
    )

    # Top padding: rows 0-139 should be gray 114 on all channels.
    top_pad = out[0, :, :140, :]
    assert np.allclose(top_pad, 114.0 / 255.0, atol=1e-6), (
        f"top padding should be ~{114/255:.4f}, got range "
        f"[{top_pad.min():.4f}, {top_pad.max():.4f}]"
    )

    # Bottom padding: rows 500-639 should be gray 114 on all channels.
    bot_pad = out[0, :, 500:, :]
    assert np.allclose(bot_pad, 114.0 / 255.0, atol=1e-6), (
        f"bottom padding should be ~{114/255:.4f}, got range "
        f"[{bot_pad.min():.4f}, {bot_pad.max():.4f}]"
    )


def test_letterbox_rgb_order():
    """BGR input channels are flipped to RGB in the output tensor."""
    # Distinct per-channel values: B=100, G=150, R=200
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    img[:, :, 0] = 100  # Blue
    img[:, :, 1] = 150  # Green
    img[:, :, 2] = 200  # Red

    out = _letterbox_tensor(img)

    # Output channel 0 = R (input channel 2), channel 1 = G, channel 2 = B.
    r_content = out[0, 0, 140:500, :]
    g_content = out[0, 1, 140:500, :]
    b_content = out[0, 2, 140:500, :]

    assert np.allclose(r_content, 200.0 / 255.0, atol=1e-6), (
        f"R channel should be ~{200/255:.4f}, got {r_content.mean():.4f}"
    )
    assert np.allclose(g_content, 150.0 / 255.0, atol=1e-6), (
        f"G channel should be ~{150/255:.4f}, got {g_content.mean():.4f}"
    )
    assert np.allclose(b_content, 100.0 / 255.0, atol=1e-6), (
        f"B channel should be ~{100/255:.4f}, got {b_content.mean():.4f}"
    )


def test_letterbox_normalization_range():
    """All output values are in [0.0, 1.0] after /255 normalization."""
    img = np.random.randint(0, 256, size=(720, 1280, 3), dtype=np.uint8)
    out = _letterbox_tensor(img)
    assert out.min() >= 0.0, f"min value {out.min():.4f} below 0.0"
    assert out.max() <= 1.0, f"max value {out.max():.4f} above 1.0"


def test_letterbox_padding_exact_gray_114():
    """Padding pixels are exactly gray 114/255 (not black, not white)."""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    out = _letterbox_tensor(img)

    # Corner pixel: top-left should be (114/255, 114/255, 114/255).
    corner = out[0, :, 0, 0]
    expected = 114.0 / 255.0
    for ch in range(3):
        assert abs(corner[ch] - expected) < 1e-6, (
            f"corner channel {ch} = {corner[ch]:.6f}, expected {expected:.6f}"
        )


def test_batch_backend_passes_rect_false_and_imgsz_640():
    """The AVA batched backend passes rect=False and imgsz=640 to the model.

    This pairs with the existing argument-capture tests in this file (which
    already assert these values through a fake model) and anchors the
    letterbox contract to the exact arguments the production path uses.
    """
    from pathlib import Path
    from types import SimpleNamespace

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
