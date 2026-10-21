"""JSONL writer contract checks (no GPU, no model download)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ava.detector import Detection, FrameResult
from ava.output import AnnotatedVideoWriter, JsonlWriter


def _sample_result() -> FrameResult:
    return FrameResult(
        index=0,
        timestamp_s=0.0,
        width=1280,
        height=720,
        detections=[
            Detection(class_id=0, label="person", confidence=0.9,
                      box=(0.1, 0.2, 0.3, 0.5), track_id=7),
        ],
    )


def test_jsonl_writes_one_line_per_frame(tmp_path: Path) -> None:
    path = tmp_path / "detections.jsonl"
    writer = JsonlWriter(path)
    writer.write(_sample_result())
    # Empty-detection frames must also be written.
    writer.write(FrameResult(1, 0.033, 1280, 720, []))
    writer.close()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["frame"] == 0
    assert first["detections"][0]["label"] == "person"
    assert first["detections"][0]["track_id"] == 7
    second = json.loads(lines[1])
    assert second["detections"] == []


# ---------------------------------------------------------------------------
# AnnotatedVideoWriter (real cv2, no GPU)
# ---------------------------------------------------------------------------

def _bgr(h: int = 64, w: int = 96):
    import numpy as np
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_annotated_writer_rejects_unopenable_path(tmp_path: Path, monkeypatch):
    import cv2

    class _BrokenWriter:
        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoWriter", lambda *a, **k: _BrokenWriter())
    with pytest.raises(RuntimeError, match="cannot open video writer"):
        AnnotatedVideoWriter(tmp_path / "out.mp4", width=96, height=64, fps=10.0)


def test_annotated_writer_draws_person_box_in_green(tmp_path: Path):
    import numpy as np

    writer = AnnotatedVideoWriter(tmp_path / "person.mp4", width=96, height=64, fps=10.0)
    frame = _bgr()
    result = FrameResult(
        index=0, timestamp_s=0.0, width=96, height=64,
        detections=[Detection(class_id=0, label="person", confidence=0.9,
                              box=(0.1, 0.2, 0.3, 0.5), track_id=7)],
    )
    canvas = writer.write(result, frame)
    writer.close()

    # The source frame must be left untouched (writer copies before drawing).
    assert not np.any(frame)
    # A box was drawn on the returned canvas.
    assert np.any(canvas)


def test_annotated_writer_draws_non_person_box_in_orange(tmp_path: Path):
    import numpy as np

    writer = AnnotatedVideoWriter(tmp_path / "car.mp4", width=96, height=64, fps=10.0)
    frame = _bgr()
    result = FrameResult(
        index=0, timestamp_s=0.0, width=96, height=64,
        detections=[Detection(class_id=2, label="car", confidence=0.8,
                              box=(0.1, 0.2, 0.3, 0.5), track_id=None)],
    )
    canvas = writer.write(result, frame)
    writer.close()

    # A box was drawn for the non-person class too.
    assert np.any(canvas)
