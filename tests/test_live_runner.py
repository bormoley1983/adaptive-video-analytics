"""Unit tests for the live multi-camera runner (mocked detector and source)."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from ava.config import (
    DetectorConfig,
    LiveCameraConfig,
    LiveRunConfig,
    RuleProfile,
)
from ava.detector import Detection, FrameResult
from ava.events import EventWriter
from ava.live_ingest import LiveSourceConfig
from ava.live_runner import CameraStats, LiveRunSummary, _CameraWorker, run_live


class FakeDetector:
    """Deterministic detector that returns one tracked detection per frame."""

    def __init__(self, device: str = "cpu") -> None:
        self.device = device
        self.load_count = 0
        self.detect_count = 0
        self._lock = threading.Lock()

    def load(self) -> None:
        self.load_count += 1

    def reset_tracker(self) -> None:
        pass

    def detect(self, frame_index: int, timestamp_s: float, bgr_frame) -> FrameResult:
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


class FakeSource:
    """Yields a fixed number of frames then stops (simulates stream end)."""

    def __init__(self, n_frames: int = 20, fps: float = 10.0) -> None:
        self.n_frames = n_frames
        self.fps = fps

    def __call__(self, config: LiveSourceConfig):
        from ava.ingest import Frame, TimeBasis

        for i in range(self.n_frames):
            yield Frame(
                index=i,
                timestamp_s=i / self.fps,
                bgr=np.zeros((480, 640, 3), dtype=np.uint8),
                capture_utc="2026-10-23T00:00:00.000Z",
                time_basis=TimeBasis.LIVE_EPOCH,
            )


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


class TestCameraWorker:
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
        """Frames beyond buffer_size are counted as dropped."""
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

        import ava.live_runner as lr

        fake_source = FakeSource(n_frames=20, fps=10.0)
        original_iter = lr.iter_live_frames
        lr.iter_live_frames = lambda cfg: fake_source(cfg)

        try:
            worker.run()
        finally:
            lr.iter_live_frames = original_iter
            writer.close()

        # 20 frames decoded, buffer holds 5, so 15 drops.
        assert worker.stats.frames_decoded == 20
        assert worker.stats.frames_dropped == 15

    def test_fps_decimation(self, tmp_path):
        """With analysis_fps much lower than source fps, inference is decimated."""
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

        # Use a fast fake source (no real-time pacing) so the test is quick.
        # The decimation uses wall-clock time, so with 10 frames arriving
        # instantly, only ~1-2 will pass the interval check.
        fake_source = FakeSource(n_frames=10, fps=10.0)
        original_iter = lr.iter_live_frames
        lr.iter_live_frames = lambda cfg: fake_source(cfg)

        try:
            worker.run()
        finally:
            lr.iter_live_frames = original_iter
            writer.close()

        # All 10 frames decoded, but inference is decimated.
        assert worker.stats.frames_decoded == 10
        # With instant frame delivery and 2 FPS target (0.5s interval),
        # only the first frame passes; subsequent frames arrive too fast.
        assert worker.stats.frames_inferred <= 3

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
