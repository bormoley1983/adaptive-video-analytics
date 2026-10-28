"""Unit tests for live RTSP ingestion (mocked OpenCV, no network)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from ava.ingest import Frame
from ava.live_ingest import (
    LiveSourceConfig,
    LiveSourceState,
    _build_gstreamer_pipeline,
    iter_live_frames,
    probe_live,
)


class FakeCapture:
    """Minimal stand-in for cv2.VideoCapture."""

    def __init__(self, frames: list[np.ndarray], fps: float = 10.0,
                 fail_after: int | None = None) -> None:
        self._frames = frames
        self._fps = fps
        self._idx = 0
        self._fail_after = fail_after
        self.opened = True

    def isOpened(self) -> bool:
        return self.opened

    def set(self, prop, value):
        return True

    def get(self, prop) -> float:
        # cv2.CAP_PROP_FPS == 5
        if prop == 5:
            return self._fps
        return 0.0

    def read(self):
        if self._fail_after is not None and self._idx >= self._fail_after:
            return False, None
        if self._idx < len(self._frames):
            frame = self._frames[self._idx]
            self._idx += 1
            return True, frame
        return False, None

    def release(self):
        self.opened = False


def _make_frames(n: int, size: tuple[int, int] = (480, 640)) -> list[np.ndarray]:
    return [np.zeros((size[0], size[1], 3), dtype=np.uint8) for _ in range(n)]


class TestBuildGstreamerPipeline:
    def test_contains_uri_and_appsink(self):
        pipeline = _build_gstreamer_pipeline("rtsp://host:8554/cam-01", queue_size=10)
        assert "uridecodebin uri=rtsp://host:8554/cam-01" in pipeline
        assert "appsink drop=true max-buffers=10" in pipeline

    def test_format_is_bgr(self):
        pipeline = _build_gstreamer_pipeline("rtsp://x", queue_size=5)
        assert "video/x-raw,format=BGR" in pipeline


class TestIterLiveFrames:
    def test_yields_frames_with_increasing_index(self, monkeypatch):
        frames = _make_frames(5)
        fake = FakeCapture(frames, fps=10.0)

        import ava.live_ingest as li
        monkeypatch.setattr(li, "_open_source", lambda cfg: (fake, 10.0))

        config = LiveSourceConfig(url="rtsp://test", reconnect_max=0)
        result = list(iter_live_frames(config))

        assert len(result) == 5
        for i, frame in enumerate(result):
            assert isinstance(frame, Frame)
            assert frame.index == i
            assert frame.bgr.shape == (480, 640, 3)
            assert frame.session_id.endswith("-s1")

    def test_timestamps_use_fps(self, monkeypatch):
        frames = _make_frames(3)
        fake = FakeCapture(frames, fps=10.0)

        import ava.live_ingest as li
        monkeypatch.setattr(li, "_open_source", lambda cfg: (fake, 10.0))

        config = LiveSourceConfig(url="rtsp://test", reconnect_max=0)
        result = list(iter_live_frames(config))

        assert result[0].timestamp_s == pytest.approx(0.0)
        assert result[1].timestamp_s == pytest.approx(0.1)
        assert result[2].timestamp_s == pytest.approx(0.2)

    def test_reconnect_on_stream_end(self, monkeypatch):
        """When the stream ends, reconnect up to reconnect_max times."""
        frames_batch = _make_frames(2)
        call_count = 0

        import ava.live_ingest as li

        def fake_open(cfg):
            nonlocal call_count
            call_count += 1
            # First two opens succeed with 2 frames; third fails.
            if call_count <= 2:
                return FakeCapture(frames_batch, fps=10.0), 10.0
            raise RuntimeError("cannot open")

        monkeypatch.setattr(li, "_open_source", fake_open)
        monkeypatch.setattr(li.time, "sleep", lambda s: None)

        config = LiveSourceConfig(url="rtsp://test", reconnect_max=2,
                                  reconnect_delay_s=0.01)
        result = list(iter_live_frames(config))

        # 2 frames from first open + 2 from second open = 4 total.
        assert len(result) == 4
        assert call_count == 3  # third attempt failed and stopped
        # Session IDs have a run-scoped prefix; check the suffix pattern.
        sids = [frame.session_id for frame in result]
        assert all(s.endswith("-s1") for s in sids[:2])
        assert all(s.endswith("-s2") for s in sids[2:])

    def test_stops_after_reconnect_budget(self, monkeypatch):
        """Exhausting reconnect_max stops the iterator."""
        import ava.live_ingest as li

        def fake_open(cfg):
            raise RuntimeError("cannot open")

        monkeypatch.setattr(li, "_open_source", fake_open)
        monkeypatch.setattr(li.time, "sleep", lambda s: None)

        config = LiveSourceConfig(url="rtsp://test", reconnect_max=1,
                                  reconnect_delay_s=0.01)
        result = list(iter_live_frames(config))

        assert result == []

    def test_zero_fps_falls_back_to_index(self, monkeypatch):
        frames = _make_frames(3)
        fake = FakeCapture(frames, fps=0.0)

        import ava.live_ingest as li
        monkeypatch.setattr(li, "_open_source", lambda cfg: (fake, 0.0))

        config = LiveSourceConfig(url="rtsp://test", reconnect_max=0)
        result = list(iter_live_frames(config))

        assert result[0].timestamp_s == pytest.approx(0.0)
        assert result[1].timestamp_s == pytest.approx(1.0)
        assert result[2].timestamp_s == pytest.approx(2.0)

    def test_reconnect_callback_fires_on_stream_end(self, monkeypatch):
        """Mid-stream disconnect invokes the callback with attempt + session."""
        frames_batch = _make_frames(2)
        call_count = 0

        import ava.live_ingest as li

        def fake_open(cfg):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return FakeCapture(frames_batch, fps=10.0), 10.0
            raise RuntimeError("cannot open")

        monkeypatch.setattr(li, "_open_source", fake_open)
        monkeypatch.setattr(li.time, "sleep", lambda s: None)

        calls: list[tuple[int, str]] = []
        config = LiveSourceConfig(
            url="rtsp://test",
            reconnect_max=2,
            reconnect_delay_s=0.01,
            on_reconnect=lambda attempt, session_id: calls.append((attempt, session_id)),
        )
        list(iter_live_frames(config))

        # One mid-stream reconnect (after first batch ends) and one open-failure
        # reconnect (third open raises). Session IDs have a run-scoped prefix.
        assert len(calls) == 2
        assert calls[0][0] == 1 and calls[0][1].endswith("-s2")
        assert calls[1][0] == 2 and calls[1][1].endswith("-s3")

    def test_reconnect_callback_fires_on_open_failure(self, monkeypatch):
        """Open failure invokes the callback before the backoff sleep."""
        import ava.live_ingest as li

        def fake_open(cfg):
            raise RuntimeError("cannot open")

        monkeypatch.setattr(li, "_open_source", fake_open)
        monkeypatch.setattr(li.time, "sleep", lambda s: None)

        calls: list[tuple[int, str]] = []
        config = LiveSourceConfig(
            url="rtsp://test",
            reconnect_max=2,
            reconnect_delay_s=0.01,
            on_reconnect=lambda attempt, session_id: calls.append((attempt, session_id)),
        )
        result = list(iter_live_frames(config))

        assert result == []
        assert len(calls) == 2
        assert calls[0][0] == 1 and calls[0][1].endswith("-s2")
        assert calls[1][0] == 2 and calls[1][1].endswith("-s3")

    def test_no_callback_without_disconnects(self, monkeypatch):
        """A clean stream that ends without reconnect budget fires no callback."""
        frames = _make_frames(3)
        fake = FakeCapture(frames, fps=10.0)

        import ava.live_ingest as li
        monkeypatch.setattr(li, "_open_source", lambda cfg: (fake, 10.0))

        calls: list[tuple[int, str]] = []
        config = LiveSourceConfig(
            url="rtsp://test",
            reconnect_max=0,
            on_reconnect=lambda attempt, session_id: calls.append((attempt, session_id)),
        )
        result = list(iter_live_frames(config))

        assert len(result) == 3
        assert calls == []

    def test_stop_event_halts_infinite_stream(self, monkeypatch):
        """A set stop event ends the iterator even though frames never run out."""
        import ava.live_ingest as li

        class InfiniteCapture:
            def __init__(self) -> None:
                self.opened = True

            def isOpened(self) -> bool:
                return self.opened

            def set(self, prop, value):
                return True

            def get(self, prop) -> float:
                if prop == 5:
                    return 10.0
                return 0.0

            def read(self):
                # Never exhausts; blocks briefly to mimic a live stream.
                time.sleep(0.01)
                return True, np.zeros((48, 64, 3), dtype=np.uint8)

            def release(self):
                self.opened = False

        monkeypatch.setattr(li, "_open_source", lambda cfg: (InfiniteCapture(), 10.0))

        stop_event = threading.Event()
        config = LiveSourceConfig(
            url="rtsp://test", reconnect_max=3, stop_event=stop_event
        )

        def _stop_soon() -> None:
            time.sleep(0.05)
            stop_event.set()

        stopper = threading.Thread(target=_stop_soon)
        stopper.start()
        result = list(iter_live_frames(config))
        stopper.join()

        # Some frames were yielded before the stop landed; the iterator ended.
        assert len(result) >= 1
        assert all(isinstance(f, Frame) for f in result)

    def test_stop_event_during_backoff_returns_promptly(self, monkeypatch):
        """A stop set during reconnect backoff ends the iterator without sleeping."""
        import ava.live_ingest as li

        def fake_open(cfg):
            raise RuntimeError("cannot open")

        monkeypatch.setattr(li, "_open_source", fake_open)

        stop_event = threading.Event()
        config = LiveSourceConfig(
            url="rtsp://test",
            reconnect_max=5,
            reconnect_delay_s=30.0,  # would hang if not interruptible
            stop_event=stop_event,
        )
        stop_event.set()

        start = time.perf_counter()
        result = list(iter_live_frames(config))
        elapsed = time.perf_counter() - start

        assert result == []
        assert elapsed < 1.0


class TestProbeLive:
    def test_ok_stream(self, monkeypatch):
        frames = _make_frames(1)
        fake = FakeCapture(frames, fps=15.0)

        import ava.live_ingest as li
        monkeypatch.setattr(li, "_open_source", lambda cfg: (fake, 15.0))

        config = LiveSourceConfig(url="rtsp://test")
        result = probe_live(config)

        assert result["ok"] is True
        assert result["width"] == 640
        assert result["height"] == 480
        assert result["fps"] == pytest.approx(15.0)

    def test_unreachable_stream(self, monkeypatch):
        import ava.live_ingest as li

        def fake_open(cfg):
            raise RuntimeError("cannot open")

        monkeypatch.setattr(li, "_open_source", fake_open)

        config = LiveSourceConfig(url="rtsp://test")
        result = probe_live(config)

        assert result["ok"] is False
        assert result["width"] == 0


class TestLiveSourceState:
    def test_initial_state(self):
        state = LiveSourceState()
        assert state.session_id == "s1"
        assert state.frame_index == 0
        assert state.reconnects == 0
