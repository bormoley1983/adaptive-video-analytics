"""Live RTSP ingestion.

Opens an RTSP stream and yields timestamped BGR frames at the stream's native
rate. Two decode backends are supported (config-selected):

- ``opencv``: simplest path; uses OpenCV's FFmpeg backend with RTSP options.
- ``gstreamer``: explicit pipeline string with buffer caps; the future NVDEC
  path plugs in here without changing the frame contract.

Both backends yield the same :class:`~ava.ingest.Frame` contract so the rest
of the pipeline (detector, rules, output) does not care which transport or
decode path produced the frame.

Reconnect: on read failure the source is closed and reopened with exponential
backoff up to ``reconnect_max`` attempts. Each successful reopen after a
failure allocates a new session identity (ADR-4 discontinuity semantics).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from .ingest import Frame, TimeBasis


def _utc_now_iso() -> str:
    """Current wall-clock UTC in ISO-8601 (capture time for live frames)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class LiveSourceConfig:
    """Parameters for one live RTSP source."""

    url: str
    backend: Literal["opencv", "gstreamer"] = "opencv"
    reconnect_max: int = 3
    reconnect_delay_s: float = 1.0
    # GStreamer-specific: maximum number of frames to buffer in the pipeline.
    gst_queue_size: int = 20


@dataclass
class LiveSourceState:
    """Mutable state for one live source connection."""

    session_id: str = "s1"
    frame_index: int = 0
    fps: float = 0.0
    reconnects: int = 0
    _cap: object = field(default=None, repr=False)


def _build_opencv_capture(url: str):
    """Create an OpenCV VideoCapture for an RTSP URL with sensible defaults."""
    import cv2  # deferred: heavy import

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    # Prefer TCP transport for loss-free loopback delivery.
    cap.set(cv2.CAP_PROP_RTSP_TRANSPORT, 0)  # 0 = TCP
    return cap


def _build_gstreamer_pipeline(url: str, queue_size: int) -> str:
    """Build a GStreamer pipeline string for RTSP decode with bounded buffers."""
    return (
        f"uridecodebin uri={url} "
        f"! video/x-raw,format=BGR "
        f"! appsink drop=true max-buffers={queue_size}"
    )


def _build_gstreamer_capture(url: str, queue_size: int):
    """Create an OpenCV VideoCapture backed by a GStreamer pipeline."""
    import cv2  # deferred: heavy import

    pipeline = _build_gstreamer_pipeline(url, queue_size)
    return cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)


def _open_source(config: LiveSourceConfig) -> tuple[object, float]:
    """Open the RTSP source and return (capture, fps).

    Raises RuntimeError if the stream cannot be opened.
    """
    if config.backend == "gstreamer":
        cap = _build_gstreamer_capture(config.url, config.gst_queue_size)
    else:
        cap = _build_opencv_capture(config.url)

    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"cannot open RTSP source: {config.url}")

    fps = cap.get(5) or 0.0  # cv2.CAP_PROP_FPS
    return cap, fps


def iter_live_frames(config: LiveSourceConfig) -> Iterator[Frame]:
    """Yield frames from a live RTSP source with reconnect support.

    The iterator runs until the caller breaks out of the loop or the source
    exhausts its reconnect budget. Each yielded frame carries a monotonically
    increasing ``index`` (across reconnects) and a source-derived timestamp.

    On reconnect, ``session_id`` advances (ADR-4: discontinuity starts a new
    session). The caller can detect this by tracking the state object.
    """
    state = LiveSourceState()
    delay = config.reconnect_delay_s

    while True:
        try:
            cap, fps = _open_source(config)
        except RuntimeError:
            if state.reconnects >= config.reconnect_max:
                return
            state.reconnects += 1
            time.sleep(delay)
            delay *= 2
            continue

        state._cap = cap
        state.fps = fps
        # Reset per-session frame index; global index continues.
        session_frame = 0

        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                ts = session_frame / fps if fps > 0 else float(session_frame)
                yield Frame(
                    index=state.frame_index,
                    timestamp_s=ts,
                    bgr=frame,
                    capture_utc=_utc_now_iso(),
                    time_basis=TimeBasis.LIVE_EPOCH,
                )
                state.frame_index += 1
                session_frame += 1
        finally:
            cap.release()

        # Stream ended or read failed; attempt reconnect.
        if state.reconnects >= config.reconnect_max:
            return
        state.reconnects += 1
        state.session_id = f"s{state.reconnects + 1}"
        time.sleep(delay)
        delay *= 2


def probe_live(config: LiveSourceConfig) -> dict:
    """Return basic stream metadata without consuming frames.

    Opens the source, reads one frame to confirm it is live, then releases.
    Returns width, height, fps and a boolean ``ok`` flag.
    """
    try:
        cap, fps = _open_source(config)
    except RuntimeError:
        return {"ok": False, "width": 0, "height": 0, "fps": 0.0}

    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return {"ok": False, "width": 0, "height": 0, "fps": fps}

    h, w = frame.shape[:2]
    return {"ok": True, "width": int(w), "height": int(h), "fps": float(fps)}
