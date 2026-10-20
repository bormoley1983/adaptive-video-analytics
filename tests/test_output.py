"""JSONL writer contract checks (no GPU, no model download)."""

from __future__ import annotations

import json
from pathlib import Path

from ava.detector import Detection, FrameResult
from ava.output import JsonlWriter


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
