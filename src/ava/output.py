"""Output writers: per-frame JSONL and annotated MP4.

Both consume the same FrameResult stream so conventional and prompted runs
produce comparable, inspectable artifacts.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .detector import FrameResult


class JsonlWriter:
    """Append one JSON object per frame to a JSONL file.

    Empty-detection frames are written too, so the record count matches the
    source frame count and timestamps stay aligned.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8")

    def write(self, result: FrameResult) -> None:
        record = {
            "frame": result.index,
            "timestamp_s": round(result.timestamp_s, 6),
            "width": result.width,
            "height": result.height,
            "detections": [asdict(d) for d in result.detections],
        }
        self._handle.write(json.dumps(record) + "\n")

    def close(self) -> None:
        self._handle.close()


class AnnotatedVideoWriter:
    """Write an annotated MP4 with boxes, labels and confidence."""

    def __init__(self, path: Path, width: int, height: int, fps: float) -> None:
        import cv2  # deferred: heavy import

        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(str(path), fourcc, fps, (width, height))
        if not self._writer.isOpened():
            raise RuntimeError(f"cannot open video writer: {path}")

    def write(self, result: FrameResult, bgr_frame) -> None:
        import cv2

        canvas = bgr_frame.copy()
        h, w = canvas.shape[:2]
        for det in result.detections:
            x1, y1, x2, y2 = (int(v * s) for v, s in zip(det.box, (w, h, w, h)))
            color = (0, 255, 0) if det.label == "person" else (255, 165, 0)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
            text = f"{det.label} {det.confidence:.2f}"
            if det.track_id is not None:
                text += f" #{det.track_id}"
            cv2.putText(canvas, text, (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        self._writer.write(canvas)

    def close(self) -> None:
        self._writer.release()
