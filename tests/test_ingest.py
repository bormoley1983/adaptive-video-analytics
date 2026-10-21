"""Video ingestion contract checks (no GPU, no real decode).

The cv2.VideoCapture boundary is faked so the owned logic — frame iteration,
timestamp derivation from index/fps, the fps fallback, and container probing —
is validated deterministically. Real hardware decode is a narrow exception
(see the benchmark report); here we exercise the adapter's control flow.
"""

from __future__ import annotations

import numpy as np
import pytest

from ava.ingest import Frame, iter_frames, probe


class _FakeCap:
    """A stand-in for cv2.VideoCapture with canned frames and properties."""

    def __init__(self, opened=True, fps=10.0, width=64, height=48, count=3,
                 n_frames=3):
        self._opened = opened
        self._fps = fps
        self._width = width
        self._height = height
        self._count = count
        self._n = n_frames
        self._i = 0
        self.released = False

    def isOpened(self):
        return self._opened

    def get(self, prop):
        # Map the cv2 property constants to canned values.
        import cv2
        if prop == cv2.CAP_PROP_FPS:
            return self._fps
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self._width
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self._height
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return self._count
        return 0.0

    def read(self):
        if self._i >= self._n:
            return False, None
        frame = np.full((self._height, self._width, 3), self._i, dtype=np.uint8)
        self._i += 1
        return True, frame

    def release(self):
        self.released = True


def _patch_cap(monkeypatch, cap):
    import cv2
    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **k: cap)


# --- iter_frames -------------------------------------------------------------


def test_iter_frames_yields_indexed_timestamped_frames(tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    cap = _FakeCap(fps=10.0, n_frames=3)
    _patch_cap(monkeypatch, cap)

    frames = list(iter_frames(src))
    assert len(frames) == 3
    assert all(isinstance(f, Frame) for f in frames)
    # Timestamps derived from index / fps.
    assert [f.index for f in frames] == [0, 1, 2]
    assert [f.timestamp_s for f in frames] == pytest.approx([0.0, 0.1, 0.2])
    # Each frame carries a BGR array of the right shape.
    assert frames[0].bgr.shape == (48, 64, 3)
    assert cap.released is True


def test_iter_frames_fps_fallback_uses_index(tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    cap = _FakeCap(fps=0.0, n_frames=2)  # fps unavailable -> ts == index
    _patch_cap(monkeypatch, cap)

    frames = list(iter_frames(src))
    assert [f.timestamp_s for f in frames] == pytest.approx([0.0, 1.0])


def test_iter_frames_unopenable_raises(tmp_path, monkeypatch):
    src = tmp_path / "missing.mp4"
    cap = _FakeCap(opened=False)
    _patch_cap(monkeypatch, cap)

    with pytest.raises(RuntimeError, match="cannot open video"):
        list(iter_frames(src))


# --- probe -------------------------------------------------------------------


def test_probe_returns_container_metadata(tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    cap = _FakeCap(fps=30.0, width=1920, height=1080, count=1768)
    _patch_cap(monkeypatch, cap)

    meta = probe(src)
    assert meta == {
        "width": 1920,
        "height": 1080,
        "fps": pytest.approx(30.0),
        "frame_count": 1768,
    }
    assert cap.released is True


def test_probe_fps_zero_reports_zero(tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    cap = _FakeCap(fps=0.0)
    _patch_cap(monkeypatch, cap)

    meta = probe(src)
    assert meta["fps"] == 0.0


def test_probe_unopenable_raises(tmp_path, monkeypatch):
    src = tmp_path / "missing.mp4"
    cap = _FakeCap(opened=False)
    _patch_cap(monkeypatch, cap)

    with pytest.raises(RuntimeError, match="cannot open video"):
        probe(src)
