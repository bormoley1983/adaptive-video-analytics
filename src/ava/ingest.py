"""Video ingestion.

Decodes an MP4 into timestamped BGR frames. Kept separate from inference so a
faster decode path (e.g. NVDEC) can replace it without touching the detector
or output writers.
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

    Timestamps use the real source presentation timestamp (PTS) where the
    container provides one (via ``CAP_PROP_POS_MSEC``); otherwise they fall
    back to frame-index / FPS (ADR-4).
    """
    import cv2  # deferred: heavy import

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    index = 0
    try:
        while True:
            # Capture the PTS of the frame about to be read. OpenCV's
            # CAP_PROP_POS_MSEC reflects the position *before* the next read
            # advances it, so we sample it here.
            pos_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
            ok, frame = cap.read()
            if not ok:
                break
            if pos_msec is not None and pos_msec > 0:
                ts = pos_msec / 1000.0
            elif fps > 0:
                ts = index / fps
            else:
                ts = float(index)
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
