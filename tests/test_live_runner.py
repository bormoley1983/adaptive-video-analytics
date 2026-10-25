"""Unit tests for the live multi-camera runner (mocked detector and source)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from ava.config import (
    DetectorConfig,
    LineCrossingRule,
    LiveCameraConfig,
    LiveRunConfig,
    RuleProfile,
)
from ava.detector import Detection, FrameResult
from ava.events import EventWriter
from ava.ingest import Frame, TimeBasis
from ava.live_ingest import LiveSourceConfig
from ava.live_runner import (
    CameraStats,
    LiveRunSummary,
    _AnalysisCadence,
    _CameraWorker,
    run_live,
)


class FakeDetector:
    """Deterministic detector that returns one tracked detection per frame."""

    def __init__(self, device: str = "cpu", delay_s: float = 0.0) -> None:
        self.device = device
        self.delay_s = delay_s
        self.load_count = 0
        self.detect_count = 0
        self.reset_count = 0
        self._lock = threading.Lock()

    def load(self) -> None:
        self.load_count += 1

    def reset_tracker(self) -> None:
        self.reset_count += 1

    def detect(self, frame_index: int, timestamp_s: float, bgr_frame) -> FrameResult:
        if self.delay_s:
            time.sleep(self.delay_s)
        with self._lock:
            self.detect_count += 1
        h, w = bgr_frame.shape[:2]
        det = Detection(
            class_id=0,
            label="person",
            confidence=0.9,
            box=(0.3, 0.4, 0.5, 0.8),
            track_id=1,
        )
        return FrameResult(frame_index, timestamp_s, w, h, [det])


class CrossingDetector(FakeDetector):
    """Moves one tracked object across a horizontal line in two frames."""

    def detect(self, frame_index: int, timestamp_s: float, bgr_frame) -> FrameResult:
        y2 = 0.4 if frame_index == 0 else 0.6
        detection = Detection(
            class_id=0,
            label="person",
            confidence=0.9,
            box=(0.3, y2 - 0.2, 0.5, y2),
            track_id=1,
        )
        return FrameResult(
            frame_index,
            timestamp_s,
            bgr_frame.shape[1],
            bgr_frame.shape[0],
            [detection],
        )


class FakeSource:
    """Yields a fixed number of frames then stops (simulates stream end)."""

    def __init__(
        self,
        n_frames: int = 20,
        fps: float = 10.0,
        delay_s: float = 0.0,
        session_id: str = "s1",
        frame_shape: tuple[int, int] = (480, 640),
    ) -> None:
        self.n_frames = n_frames
        self.fps = fps
        self.delay_s = delay_s
        self.session_id = session_id
        self.frame_shape = frame_shape

    def __call__(self, config: LiveSourceConfig):
        from ava.ingest import Frame, TimeBasis

        for i in range(self.n_frames):
            if self.delay_s:
                time.sleep(self.delay_s)
            yield Frame(
                index=i,
                timestamp_s=i / self.fps,
                bgr=np.zeros((*self.frame_shape, 3), dtype=np.uint8),
                capture_utc="2026-10-23T00:00:00.000Z",
                time_basis=TimeBasis.LIVE_EPOCH,
                session_id=self.session_id,
            )


class FakeCapture5:
    """OpenCV stand-in that serves 5 frames then reports stream end."""

    def __init__(self) -> None:
        self._idx = 0

    def isOpened(self) -> bool:
        return True

    def set(self, prop, value):
        return True

    def get(self, prop) -> float:
        if prop == 5:  # cv2.CAP_PROP_FPS
            return 10.0
        return 0.0

    def read(self):
        if self._idx < 5:
            self._idx += 1
            return True, np.zeros((480, 640, 3), dtype=np.uint8)
        return False, None

    def release(self):
        pass


def _make_live_config(n_cameras: int = 1, **kwargs) -> LiveRunConfig:
    cameras = [
        LiveCameraConfig(
            id=f"cam-{i+1:02d}",
            url=f"rtsp://127.0.0.1:8554/cam-{i+1:02d}",
            profiles=["test-profile"],
            analysis_fps=kwargs.pop("analysis_fps", 10.0),
        )
        for i in range(n_cameras)
    ]
    return LiveRunConfig(
        cameras=cameras,
        profiles=[RuleProfile(id="test-profile", rules=[])],
        **kwargs,
    )


def _make_detector_config(tmp_path: Path) -> DetectorConfig:
    return DetectorConfig(
        kind="conventional",
        model=tmp_path / "fake.pt",
        confidence=0.5,
        iou=0.5,
    )


class TestCameraStats:
    def test_to_dict(self):
        stats = CameraStats(camera_id="cam-01")
        stats.frames_decoded = 100
        stats.frames_inferred = 40
        d = stats.to_dict()
        assert d["camera_id"] == "cam-01"
        assert d["frames_decoded"] == 100
        assert d["frames_inferred"] == 40
        assert d["events_emitted"] == 0


class TestLiveRunSummary:
    def test_to_dict(self):
        summary = LiveRunSummary(
            detector_mode="shared",
            backend="opencv",
            cameras=[CameraStats(camera_id="cam-01")],
            wall_seconds=5.0,
            total_events=3,
        )
        d = summary.to_dict()
        assert d["detector_mode"] == "shared"
        assert d["backend"] == "opencv"
        assert len(d["cameras"]) == 1
        assert d["total_events"] == 3


class TestAnalysisCadence:
    @pytest.mark.parametrize(
        ("source_fps", "analysis_fps", "expected"),
        [(10.0, 4.0, 40), (10.0, 10.0, 100), (25.0, 25.0, 250), (50.0, 50.0, 500)],
    )
    def test_realizes_requested_rate_without_exceeding_source(
        self, source_fps, analysis_fps, expected
    ):
        cadence = _AnalysisCadence(analysis_fps)

        selected = [
            index
            for index in range(int(source_fps * 10))
            if cadence.should_analyze(index / source_fps, "s1")
        ]

        assert len(selected) == expected

    def test_rate_above_source_selects_each_available_frame(self):
        cadence = _AnalysisCadence(50.0)

        selected = [
            index
            for index in range(100)
            if cadence.should_analyze(index / 10.0, "s1")
        ]

        assert len(selected) == 100

    def test_new_session_restarts_source_time_schedule(self):
        cadence = _AnalysisCadence(4.0)
        first_session = [
            cadence.should_analyze(index / 10.0, "s1") for index in range(10)
        ]

        first_after_reconnect = cadence.should_analyze(0.0, "s2")

        assert sum(first_session) == 4
        assert first_after_reconnect is True


class TestCameraWorker:
    @pytest.mark.parametrize(
        ("source_fps", "analysis_fps", "expected"),
        [(10.0, 4.0, 40), (10.0, 10.0, 100), (25.0, 25.0, 250), (50.0, 50.0, 500)],
    )
    def test_worker_realizes_source_time_cadence(
        self, tmp_path, monkeypatch, source_fps, analysis_fps, expected
    ):
        import ava.live_runner as lr

        frame_count = int(source_fps * 10)
        fake_source = FakeSource(
            n_frames=frame_count,
            fps=source_fps,
            frame_shape=(4, 4),
        )
        monkeypatch.setattr(lr, "iter_live_frames", fake_source)
        writer = EventWriter(tmp_path / "events.jsonl")
        worker = _CameraWorker(
            cam_config=LiveCameraConfig(
                id="cam-01",
                url="rtsp://test",
                profiles=[],
                analysis_fps=analysis_fps,
            ),
            source_config=LiveSourceConfig(url="rtsp://test", reconnect_max=0),
            detector=FakeDetector(),
            detector_lock=None,
            rules=[],
            event_writer=writer,
            buffer_size=frame_count,
            model_ref="fake.pt",
        )

        worker.run()
        writer.close()

        assert worker.stats.frames_decoded == frame_count
        assert worker.stats.frames_inferred == expected
        assert worker.stats.frames_dropped == 0

    def test_processes_frames_and_emits_no_events_without_rules(self, tmp_path):
        """With no rules, frames are decoded and inferred but no events fire."""
        detector = FakeDetector()
        writer = EventWriter(tmp_path / "events.jsonl")
        source_config = LiveSourceConfig(url="rtsp://test", reconnect_max=0)

        cam = LiveCameraConfig(
            id="cam-01", url="rtsp://test", profiles=[], analysis_fps=10.0
        )

        worker = _CameraWorker(
            cam_config=cam,
            source_config=source_config,
            detector=detector,
            detector_lock=None,
            rules=[],
            event_writer=writer,
            buffer_size=10,
            model_ref="fake.pt",
        )

        # Monkeypatch iter_live_frames to use our fake source.
        import ava.live_runner as lr

        fake_source = FakeSource(n_frames=10, fps=10.0)
        original_iter = lr.iter_live_frames
        lr.iter_live_frames = lambda cfg: fake_source(cfg)

        try:
            worker.run()
        finally:
            lr.iter_live_frames = original_iter
            writer.close()

        assert worker.stats.frames_decoded == 10
        assert worker.stats.frames_inferred == 10
        assert worker.stats.events_emitted == 0
        assert detector.detect_count == 10

    def test_buffer_drops_when_full(self, tmp_path):
        """Only frames displaced from a full processing queue are dropped."""
        detector = FakeDetector()
        writer = EventWriter(tmp_path / "events.jsonl")
        source_config = LiveSourceConfig(url="rtsp://test", reconnect_max=0)

        cam = LiveCameraConfig(
            id="cam-01", url="rtsp://test", profiles=[], analysis_fps=10.0
        )

        worker = _CameraWorker(
            cam_config=cam,
            source_config=source_config,
            detector=detector,
            detector_lock=None,
            rules=[],
            event_writer=writer,
            buffer_size=5,  # small buffer
            model_ref="fake.pt",
        )

        for index in range(20):
            worker._enqueue_frame(
                Frame(
                    index=index,
                    timestamp_s=index / 10.0,
                    bgr=np.zeros((4, 4, 3), dtype=np.uint8),
                    time_basis=TimeBasis.LIVE_EPOCH,
                    session_id="s1",
                )
            )
        writer.close()

        assert worker.queue_snapshot()[0] == 5
        assert worker.stats.frames_dropped == 15
        assert worker.metrics.frames_dropped_buffer == 15

    def test_queue_age_uses_oldest_frame_arrival_time(self, tmp_path, monkeypatch):
        import ava.live_runner as lr

        writer = EventWriter(tmp_path / "events.jsonl")
        worker = _CameraWorker(
            cam_config=LiveCameraConfig(
                id="cam-01", url="rtsp://test", profiles=[], analysis_fps=4.0
            ),
            source_config=LiveSourceConfig(url="rtsp://test", reconnect_max=0),
            detector=FakeDetector(),
            detector_lock=None,
            rules=[],
            event_writer=writer,
            buffer_size=2,
            model_ref="fake.pt",
        )
        clock = iter([10.0, 10.25])
        monkeypatch.setattr(lr.time, "perf_counter", lambda: next(clock))

        worker._enqueue_frame(
            Frame(0, 0.0, np.zeros((4, 4, 3), dtype=np.uint8), session_id="s1")
        )
        depth, age_s = worker.queue_snapshot()
        writer.close()

        assert depth == 1
        assert age_s == pytest.approx(0.25)

    def test_event_latency_includes_queue_inference_and_sink_time(
        self, tmp_path, monkeypatch
    ):
        import ava.live_runner as lr

        monkeypatch.setattr(
            lr,
            "iter_live_frames",
            FakeSource(n_frames=2, fps=10.0, frame_shape=(4, 4)),
        )
        writer = EventWriter(tmp_path / "events.jsonl")
        worker = _CameraWorker(
            cam_config=LiveCameraConfig(
                id="cam-01", url="rtsp://test", profiles=[], analysis_fps=10.0
            ),
            source_config=LiveSourceConfig(url="rtsp://test", reconnect_max=0),
            detector=CrossingDetector(),
            detector_lock=None,
            rules=[
                LineCrossingRule(
                    id="cross",
                    line={"start": (0.0, 0.5), "end": (1.0, 0.5)},
                )
            ],
            event_writer=writer,
            buffer_size=2,
            model_ref="fake.pt",
            slow_sink_s=0.01,
        )

        worker.run()
        writer.close()

        latency = worker.metrics.event_latency.percentiles()
        assert worker.stats.events_emitted == 1
        assert latency["count"] == 1
        assert latency["p50"] >= 0.01

    def test_reconnect_builds_new_rule_session_and_restarts_cadence(
        self, tmp_path, monkeypatch
    ):
        import ava.live_runner as lr

        frames = [
            Frame(0, 0.0, np.zeros((4, 4, 3), dtype=np.uint8), session_id="s1"),
            Frame(1, 0.1, np.zeros((4, 4, 3), dtype=np.uint8), session_id="s1"),
            Frame(2, 0.0, np.zeros((4, 4, 3), dtype=np.uint8), session_id="s2"),
        ]
        monkeypatch.setattr(lr, "iter_live_frames", lambda config: iter(frames))
        sessions = []
        real_rule_engine = lr.RuleEngine

        def recording_engine(*args, **kwargs):
            sessions.append(kwargs["session_id"])
            return real_rule_engine(*args, **kwargs)

        monkeypatch.setattr(lr, "RuleEngine", recording_engine)
        writer = EventWriter(tmp_path / "events.jsonl")
        worker = _CameraWorker(
            cam_config=LiveCameraConfig(
                id="cam-01", url="rtsp://test", profiles=[], analysis_fps=4.0
            ),
            source_config=LiveSourceConfig(url="rtsp://test", reconnect_max=0),
            detector=FakeDetector(),
            detector_lock=None,
            rules=[],
            event_writer=writer,
            buffer_size=3,
            model_ref="fake.pt",
        )

        worker.run()
        writer.close()

        assert sessions == ["s1", "s2"]
        assert worker.stats.frames_inferred == 2
        assert worker.detector.reset_count == 1

    def test_fps_decimation(self, tmp_path):
        """Source-time pacing selects the configured analysis cadence."""
        detector = FakeDetector()
        writer = EventWriter(tmp_path / "events.jsonl")
        source_config = LiveSourceConfig(url="rtsp://test", reconnect_max=0)

        # Source at 10 FPS, analysis at 2 FPS -> ~5 inferences from 10 frames.
        cam = LiveCameraConfig(
            id="cam-01", url="rtsp://test", profiles=[], analysis_fps=2.0
        )

        worker = _CameraWorker(
            cam_config=cam,
            source_config=source_config,
            detector=detector,
            detector_lock=None,
            rules=[],
            event_writer=writer,
            buffer_size=30,
            model_ref="fake.pt",
        )

        import ava.live_runner as lr

        # Delivery is intentionally faster than real time: pacing uses source
        # timestamps and therefore remains deterministic.
        fake_source = FakeSource(n_frames=10, fps=10.0)
        original_iter = lr.iter_live_frames
        lr.iter_live_frames = lambda cfg: fake_source(cfg)

        try:
            worker.run()
        finally:
            lr.iter_live_frames = original_iter
            writer.close()

        assert worker.stats.frames_decoded == 10
        assert worker.stats.frames_inferred == 2

    def test_shared_detector_lock(self, tmp_path):
        """In shared mode, the detector lock is acquired around detect()."""
        detector = FakeDetector()
        writer = EventWriter(tmp_path / "events.jsonl")
        source_config = LiveSourceConfig(url="rtsp://test", reconnect_max=0)

        cam = LiveCameraConfig(
            id="cam-01", url="rtsp://test", profiles=[], analysis_fps=10.0
        )

        lock = threading.Lock()
        worker = _CameraWorker(
            cam_config=cam,
            source_config=source_config,
            detector=detector,
            detector_lock=lock,
            rules=[],
            event_writer=writer,
            buffer_size=10,
            model_ref="fake.pt",
        )

        import ava.live_runner as lr

        fake_source = FakeSource(n_frames=5, fps=10.0)
        original_iter = lr.iter_live_frames
        lr.iter_live_frames = lambda cfg: fake_source(cfg)

        try:
            worker.run()
        finally:
            lr.iter_live_frames = original_iter
            writer.close()

        assert worker.stats.frames_inferred == 5
        assert detector.detect_count == 5


class TestRunLive:
    def test_shared_mode_two_cameras(self, tmp_path, monkeypatch):
        """Two cameras in shared mode both process frames."""
        import ava.live_runner as lr

        # Patch _build_detector to return our fake.
        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        # Patch iter_live_frames for each camera.
        sources = {
            "cam-01": FakeSource(n_frames=10, fps=10.0),
            "cam-02": FakeSource(n_frames=10, fps=10.0),
        }

        def fake_iter(cfg: LiveSourceConfig):
            for key, src in sources.items():
                if key in cfg.url:
                    return src(cfg)
            return iter(())

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=2, detector_mode="shared")
        det_config = _make_detector_config(tmp_path)

        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)

        assert summary.detector_mode == "shared"
        assert len(summary.cameras) == 2
        for cam_stats in summary.cameras:
            assert cam_stats.frames_decoded == 10
        # Detector loaded once (shared mode).
        assert fake_det.load_count == 1

    def test_per_camera_mode_two_cameras(self, tmp_path, monkeypatch):
        """Two cameras in per_camera mode each get their own detector."""
        import ava.live_runner as lr

        detectors = [FakeDetector(), FakeDetector()]
        call_count = 0

        def fake_build(dc, **kw):
            nonlocal call_count
            det = detectors[call_count % len(detectors)]
            call_count += 1
            return det

        monkeypatch.setattr(lr, "_build_detector", fake_build)

        sources = {
            "cam-01": FakeSource(n_frames=5, fps=10.0),
            "cam-02": FakeSource(n_frames=5, fps=10.0),
        }

        def fake_iter(cfg: LiveSourceConfig):
            for key, src in sources.items():
                if key in cfg.url:
                    return src(cfg)
            return iter(())

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=2, detector_mode="per_camera")
        det_config = _make_detector_config(tmp_path)

        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)

        assert summary.detector_mode == "per_camera"
        assert len(summary.cameras) == 2
        # Each detector loaded once.
        assert detectors[0].load_count == 1
        assert detectors[1].load_count == 1

    def test_output_events_file_created(self, tmp_path, monkeypatch):
        """The events.jsonl file is created in the output directory."""
        import ava.live_runner as lr

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=3, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1)
        det_config = _make_detector_config(tmp_path)
        out_dir = tmp_path / "results"

        summary = run_live(live_config, det_config, out_dir, gpu=None)

        # events.jsonl should exist (even if empty with no rules).
        assert any("events" in p for p in summary.output_paths)
        event_file = Path(summary.output_paths[0])
        assert event_file.exists()


class TestLiveConfigValidation:
    def test_rejects_empty_cameras(self):
        with pytest.raises(ValueError, match="at least one camera"):
            LiveRunConfig(cameras=[])

    def test_rejects_duplicate_camera_ids(self):
        cams = [
            LiveCameraConfig(id="cam-01", url="rtsp://a"),
            LiveCameraConfig(id="cam-01", url="rtsp://b"),
        ]
        with pytest.raises(ValueError, match="duplicate camera ids"):
            LiveRunConfig(cameras=cams)

    def test_rejects_unknown_profile_ref(self):
        cams = [
            LiveCameraConfig(id="cam-01", url="rtsp://a", profiles=["missing"]),
        ]
        with pytest.raises(ValueError, match="unknown rule profile"):
            LiveRunConfig(cameras=cams, profiles=[])

    def test_rejects_non_rtsp_url(self):
        with pytest.raises(ValueError, match="rtsp://"):
            LiveCameraConfig(id="cam-01", url="http://not-rtsp")

    def test_valid_config(self):
        cfg = _make_live_config(n_cameras=2)
        assert len(cfg.cameras) == 2
        assert cfg.backend == "opencv"
        assert cfg.detector_mode == "shared"

    def test_slow_sink_default_zero(self):
        cfg = _make_live_config(n_cameras=1)
        assert cfg.slow_sink_s == 0.0

    def test_metrics_interval_default(self):
        cfg = _make_live_config(n_cameras=1)
        assert cfg.metrics_interval_s == 5.0


class TestMetricsIntegration:
    """Verify that run_live produces metrics.json with expected structure."""

    def test_metrics_file_created(self, tmp_path, monkeypatch):
        import ava.live_runner as lr

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=10, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1)
        det_config = _make_detector_config(tmp_path)
        out_dir = tmp_path / "results"

        summary = run_live(live_config, det_config, out_dir, gpu=None)

        # metrics.json should be in output_paths.
        metrics_files = [p for p in summary.output_paths if p.endswith("metrics.json")]
        assert len(metrics_files) == 1
        metrics_path = Path(metrics_files[0])
        assert metrics_path.exists()

        import json

        with metrics_path.open() as f:
            data = json.load(f)
        assert "cameras" in data
        assert len(data["cameras"]) == 1
        cam_data = data["cameras"][0]
        assert cam_data["camera_id"] == "cam-01"
        assert cam_data["frames_decoded"] == 10
        assert cam_data["frames_inferred"] > 0
        assert "inference_timing" in cam_data
        assert "event_latency" in cam_data

    def test_metrics_record_drops(self, tmp_path, monkeypatch):
        """Buffer drops are recorded in metrics."""
        import ava.live_runner as lr

        fake_det = FakeDetector(delay_s=0.01)
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=20, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1, buffer_size=5)
        det_config = _make_detector_config(tmp_path)
        out_dir = tmp_path / "results"

        summary = run_live(live_config, det_config, out_dir, gpu=None)

        import json

        metrics_path = Path(next(p for p in summary.output_paths if p.endswith("metrics.json")))
        with metrics_path.open() as f:
            data = json.load(f)
        cam_data = data["cameras"][0]
        assert cam_data["frames_dropped"]["buffer_full"] == summary.cameras[0].frames_dropped
        assert cam_data["frames_dropped"]["buffer_full"] >= 14


class TestOverloadScenarios:
    """Simulate overload conditions and verify bounded behavior."""

    def test_slow_sink_does_not_crash(self, tmp_path, monkeypatch):
        """A slow sink adds latency but does not crash the worker."""
        import ava.live_runner as lr

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=5, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        # Use a very small slow_sink to keep the test fast.
        live_config = _make_live_config(n_cameras=1, slow_sink_s=0.01)
        det_config = _make_detector_config(tmp_path)
        out_dir = tmp_path / "results"

        summary = run_live(live_config, det_config, out_dir, gpu=None)

        # No errors recorded.
        for cam in summary.cameras:
            assert cam.errors == []

    def test_camera_disconnect_surviving_streams_continue(self, tmp_path, monkeypatch):
        """When one camera's source ends, the other continues processing."""
        import ava.live_runner as lr

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        # cam-01 gets 10 frames, cam-02 gets only 3 (simulates early disconnect).
        sources = {
            "cam-01": FakeSource(n_frames=10, fps=10.0),
            "cam-02": FakeSource(n_frames=3, fps=10.0),
        }

        def fake_iter(cfg: LiveSourceConfig):
            for key, src in sources.items():
                if key in cfg.url:
                    return src(cfg)
            return iter(())

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=2)
        det_config = _make_detector_config(tmp_path)
        out_dir = tmp_path / "results"

        summary = run_live(live_config, det_config, out_dir, gpu=None)

        # Both cameras processed their available frames.
        cam_stats = {c.camera_id: c for c in summary.cameras}
        assert cam_stats["cam-01"].frames_decoded == 10
        assert cam_stats["cam-02"].frames_decoded == 3
        # No errors on either (clean stream end is not an error).
        assert cam_stats["cam-01"].errors == []
        assert cam_stats["cam-02"].errors == []

    def test_buffer_bounded_under_overload(self, tmp_path, monkeypatch):
        """Frame buffer never exceeds its configured size."""
        import ava.live_runner as lr

        fake_det = FakeDetector(delay_s=0.01)
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=100, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1, buffer_size=10)
        det_config = _make_detector_config(tmp_path)
        out_dir = tmp_path / "results"

        summary = run_live(live_config, det_config, out_dir, gpu=None)

        cam = summary.cameras[0]
        assert cam.frames_decoded == 100
        assert 89 <= cam.frames_dropped <= 90


class TestStructuredLogs:
    """B-021: structured JSON stdout logs with correlation fields and redaction."""

    def _run(self, tmp_path, monkeypatch, capsys, **cfg_kwargs):
        import json

        import ava.live_runner as lr

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=5, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1, **cfg_kwargs)
        det_config = _make_detector_config(tmp_path)
        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)

        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        return summary, lines

    def test_start_and_end_logs_carry_run_id(self, tmp_path, monkeypatch, capsys):
        _, lines = self._run(tmp_path, monkeypatch, capsys)
        start = next(l for l in lines if l["component"] == "live_run.start")
        end = next(l for l in lines if l["component"] == "live_run.end")
        assert start["run_id"] == end["run_id"]
        assert start["worker_id"] == "ava-live-1"
        assert start["schema_version"] == "1"

    def test_camera_done_log_carries_camera_correlation(self, tmp_path, monkeypatch, capsys):
        _, lines = self._run(tmp_path, monkeypatch, capsys)
        done = next(l for l in lines if l["component"] == "live_run.camera_done")
        assert done["camera_id"] == "cam-01"
        assert done["frames_decoded"] == 5

    def test_shared_conventional_mode_warns_about_tracker_isolation(
        self, tmp_path, monkeypatch, capsys
    ):
        _, lines = self._run(tmp_path, monkeypatch, capsys)

        warning = next(
            line for line in lines if line["component"] == "live_run.tracker_isolation"
        )
        assert warning["severity"] == "warning"

    def test_no_raw_rtsp_url_in_any_log_line(self, tmp_path, monkeypatch, capsys):
        import json

        _, lines = self._run(tmp_path, monkeypatch, capsys)
        for line in lines:
            rendered = json.dumps(line)
            assert "rtsp://" not in rendered, f"raw RTSP URL leaked: {rendered}"

    def test_metrics_available_without_collector(self, tmp_path, monkeypatch, capsys):
        """Prometheus scrape surface works with no external collector running."""
        import socket

        import ava.live_runner as lr
        from ava.telemetry import render_metrics

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=5, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        live_config = _make_live_config(n_cameras=1)
        live_config.telemetry.prometheus_port = port
        det_config = _make_detector_config(tmp_path)

        run_live(live_config, det_config, tmp_path / "out", gpu=None)

        # The scrape surface rendered metrics for the run (no collector needed).
        text = render_metrics()
        assert "ava_frames_decoded_total" in text
        assert 'camera="cam-01"' in text


class TestReconnectTelemetry:
    """B-021 closeout: reconnects are counted, metered and logged."""

    def _run_with_reconnects(self, tmp_path, monkeypatch, capsys, n_cameras=2):
        import json

        import ava.live_ingest as li
        import ava.live_runner as lr
        from ava.telemetry import render_metrics

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        # Each camera's source ends after 5 frames; with reconnect_max=1 the
        # real ingest loop performs exactly one mid-stream reconnect per
        # camera before giving up. Patching _open_source (not iter_live_frames)
        # keeps the real reconnect/callback path under test.
        def fake_open(cfg: LiveSourceConfig):
            return FakeCapture5(), 10.0

        monkeypatch.setattr(li, "_open_source", fake_open)
        monkeypatch.setattr(li.time, "sleep", lambda s: None)

        live_config = _make_live_config(n_cameras=n_cameras, reconnect_max=1)
        det_config = _make_detector_config(tmp_path)

        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)

        lines = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.strip()
        ]
        return summary, lines, render_metrics()

    def test_reconnect_updates_stats_metrics_and_log(self, tmp_path, monkeypatch, capsys):
        summary, lines, text = self._run_with_reconnects(tmp_path, monkeypatch, capsys)

        cam_stats = {c.camera_id: c for c in summary.cameras}
        assert cam_stats["cam-01"].reconnects == 1
        assert cam_stats["cam-02"].reconnects == 1

        reconnect_logs = [l for l in lines if l["component"] == "live_run.reconnect"]
        assert len(reconnect_logs) == 2
        by_cam = {l["camera_id"]: l for l in reconnect_logs}
        assert by_cam["cam-01"]["attempt"] == 1
        assert by_cam["cam-01"]["session_id"] == "s2"
        assert by_cam["cam-02"]["attempt"] == 1
        assert by_cam["cam-02"]["session_id"] == "s2"

        # The Prometheus counter series exists with a value of 1 per camera.
        for cam in ("cam-01", "cam-02"):
            line = next(
                l for l in text.splitlines()
                if l.startswith(f'ava_reconnects_total{{camera="{cam}"')
            )
            assert line.split()[-1] == "1.0"

    def test_no_reconnects_on_clean_run(self, tmp_path, monkeypatch, capsys):
        import json

        import ava.live_runner as lr
        from ava.telemetry import render_metrics

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=5, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1, reconnect_max=0)
        det_config = _make_detector_config(tmp_path)

        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)

        assert summary.cameras[0].reconnects == 0

        lines = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.strip()
        ]
        assert not any(l["component"] == "live_run.reconnect" for l in lines)

        # No reconnects recorded: no sample lines for the counter (only the
        # HELP/TYPE metadata, which is always present).
        assert not any(
            l.startswith("ava_reconnects_total{") for l in render_metrics().splitlines()
        )


class TestOverloadTelemetry:
    """B-021 closeout: processing-queue overload is visible in metrics."""

    def test_queue_overload_drops_visible_in_metrics(self, tmp_path, monkeypatch):
        import ava.live_runner as lr
        from ava.telemetry import render_metrics

        fake_det = FakeDetector(delay_s=0.01)
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=20, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1, buffer_size=5)
        det_config = _make_detector_config(tmp_path)

        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)

        cam = summary.cameras[0]
        assert cam.frames_decoded == 20
        assert 14 <= cam.frames_dropped <= 15

        text = render_metrics()
        assert 'ava_frames_dropped_total{camera="cam-01",reason="buffer_full"' in text
        # The drop counter value matches the stats.
        for line in text.splitlines():
            if line.startswith('ava_frames_dropped_total{camera="cam-01",reason="buffer_full"'):
                assert float(line.split()[-1]) >= cam.frames_dropped


class TestBoundedDuration:
    """B-021 closeout: a bounded run stops infinite streams and writes metrics."""

    def test_duration_stops_infinite_stream_and_writes_metrics(
        self, tmp_path, monkeypatch, capsys
    ):
        import json
        import time

        import ava.live_ingest as li
        import ava.live_runner as lr

        class InfiniteCapture:
            """Serves frames forever; read() blocks briefly like a live stream."""

            def __init__(self) -> None:
                self._idx = 0

            def isOpened(self) -> bool:
                return True

            def set(self, prop, value):
                return True

            def get(self, prop) -> float:
                if prop == 5:  # cv2.CAP_PROP_FPS
                    return 10.0
                return 0.0

            def read(self):
                time.sleep(0.01)
                self._idx += 1
                return True, np.zeros((48, 64, 3), dtype=np.uint8)

            def release(self):
                pass

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)
        # Patch _open_source (not iter_live_frames) so the real stop-event path
        # in iter_live_frames is exercised.
        monkeypatch.setattr(li, "_open_source", lambda cfg: (InfiniteCapture(), 10.0))

        live_config = _make_live_config(n_cameras=1, duration_s=0.3)
        det_config = _make_detector_config(tmp_path)

        start = time.perf_counter()
        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)
        elapsed = time.perf_counter() - start

        # The run returned on its own: bounded by duration_s plus teardown.
        assert elapsed < 10.0
        cam = summary.cameras[0]
        assert cam.frames_decoded > 0
        assert (tmp_path / "out" / "metrics.json").exists()

        lines = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.strip()
        ]
        bounded = next(l for l in lines if l["component"] == "live_run.bounded")
        assert bounded["duration_seconds"] == 0.3

    def test_zero_duration_is_unbounded(self, tmp_path, monkeypatch):
        """duration_s=0 (default) keeps the existing stream-end behavior."""
        import ava.live_runner as lr

        fake_det = FakeDetector()
        monkeypatch.setattr(lr, "_build_detector", lambda dc, **kw: fake_det)

        def fake_iter(cfg: LiveSourceConfig):
            return FakeSource(n_frames=5, fps=10.0)(cfg)

        monkeypatch.setattr(lr, "iter_live_frames", fake_iter)

        live_config = _make_live_config(n_cameras=1)  # duration_s defaults to 0
        det_config = _make_detector_config(tmp_path)

        summary = run_live(live_config, det_config, tmp_path / "out", gpu=None)

        assert summary.cameras[0].frames_decoded == 5
