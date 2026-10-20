"""Video ingestion.

Decodes an MP4 into timestamped BGR frames. Kept separate from inference so a
faster decode path (e.g. NVDEC in E-3) can replace it without touching the
detector or output writers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Frame:
    """One decoded frame with its source timestamp."""

    index: int
    timestamp_s: float
    bgr: object  # numpy.ndarray, typed loosely to avoid a hard cv2 import here


def iter_frames(source: Path):
    """Yield frames from a video file in decode order.

    Timestamps are derived from the frame index and the container FPS so they
    can be matched back to the source timeline (E-1-T3 acceptance).
    """
    import cv2  # deferred: heavy import

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            ts = (index / fps) if fps > 0 else float(index)
            yield Frame(index=index, timestamp_s=ts, bgr=frame)
            index += 1
    finally:
        cap.release()


def probe(source: Path) -> dict:
    """Return basic container metadata for the run manifest."""
    import cv2

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {source}")
    try:
        return {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
            "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        }
    finally:
        cap.release()
