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

import threading
import time
from collections.abc import Callable, Iterator
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
    # Optional hook invoked on every reconnect with (attempt, new_session_id).
    # Attempt is 1-based; session_id is the identity that will be used for
    # frames after this reconnect (ADR-4 discontinuity semantics).
    on_reconnect: Callable[[int, str], None] | None = None
    # Optional stop signal checked between frame reads and during backoff.
    # When set, the iterator stops yielding and returns cleanly.
    stop_event: threading.Event | None = None


@dataclass
class LiveSourceState:
    """Mutable state for one live source connection."""

    session_id: str = "s1"
    frame_index: int = 0
    fps: float = 0.0
    reconnects: int = 0
    _cap: object = field(default=None, repr=False)


def make_session_id() -> str:
    """Unique per-run session prefix; prevents cross-run event ID collisions."""
    import uuid

    return uuid.uuid4().hex[:12]


def _build_opencv_capture(url: str):
    """Create an OpenCV VideoCapture for an RTSP URL with sensible defaults."""
    import cv2  # deferred: heavy import

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    # Prefer TCP transport for loss-free loopback delivery. The property is only
    # exposed by FFmpeg builds that patch it in; stock OpenCV wheels omit it, so
    # guard with hasattr and fall back to the backend default (UDP).
    if hasattr(cv2, "CAP_PROP_RTSP_TRANSPORT"):
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
    base_session = make_session_id()
    state = LiveSourceState(session_id=f"{base_session}-s1")
    delay = config.reconnect_delay_s
    stop_event = config.stop_event

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    while True:
        if _stopped():
            return
        try:
            cap, fps = _open_source(config)
        except RuntimeError:
            if state.reconnects >= config.reconnect_max or _stopped():
                return
            state.reconnects += 1
            state.session_id = f"{base_session}-s{state.reconnects + 1}"
            if config.on_reconnect is not None:
                config.on_reconnect(state.reconnects, state.session_id)
            # Interruptible backoff so a stop signal lands promptly.
            if stop_event is not None and stop_event.wait(delay):
                return
            delay *= 2
            continue

        state._cap = cap
        state.fps = fps
        # Reset per-session frame index; global index continues.
        session_frame = 0

        try:
            while True:
                if _stopped():
                    break
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
                    session_id=state.session_id,
                )
                state.frame_index += 1
                session_frame += 1
        finally:
            cap.release()

        # Stream ended, read failed, or stopped; attempt reconnect.
        if _stopped() or state.reconnects >= config.reconnect_max:
            return
        state.reconnects += 1
        state.session_id = f"{base_session}-s{state.reconnects + 1}"
        if config.on_reconnect is not None:
            config.on_reconnect(state.reconnects, state.session_id)
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
