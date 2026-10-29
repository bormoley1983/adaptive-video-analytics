import json
import time
from pathlib import Path

import pytest

from ava.batch import BatchItemResult
from ava.batch_live import run_batched_live
from ava.config import DetectorConfig, LiveRunConfig
from ava.detector import FrameResult
from ava.ingest import Frame

pytestmark = pytest.mark.integration


class Backend:
    def __init__(self):
        self.arithmetic = {}
    def load(self):
        pass
    def detect_batch(self, batch):
        return tuple(BatchItemResult(i.key, 10, 10) for i in batch.items)
    def close(self):
        pass


class Tracker:
    def __init__(self, fps):
        self.session = None
    def reset(self, session):
        self.session = session
    def update(self, item, result):
        assert item.key.session_id == self.session
        return FrameResult(item.key.frame_index, item.frame.timestamp_s, 10, 10, [])


class Sink:
    def write(self, record):
        return True


def source(config):
    index = 0
    while not config.stop_event.is_set():
        yield Frame(index, index / 100., None, session_id="s")
        index += 1
        time.sleep(.01)


def config():
    return LiveRunConfig(detector_mode="batched", duration_s=.12,
        persistence={"backend": "jsonl"}, batch={"warmup_s": .02, "max_wait_ms": 1,
            "readiness_timeout_s": 1, "shutdown_timeout_s": 1},
        cameras=[{"id": c, "url": f"rtsp://example/{c}", "analysis_fps": 100} for c in "ab"])


def test_common_window_and_terminal_ledgers(tmp_path):
    summary = run_batched_live(config(), DetectorConfig(model=Path("unused.pt")), tmp_path,
                               None, Sink(), source=source, backend_factory=Backend,
                               tracker_factory=Tracker)
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert summary.total_events == 0
    assert report["status"] == "completed"
    assert report["unresolved_threads"] == []
    assert report["measurement_window"][1] - report["measurement_window"][0] == pytest.approx(.12)
    for camera in report["cameras"]:
        assert camera["frames_inferred"] > 0
        assert sum(camera["terminal"].values()) == camera["frames_decoded"]
        assert sum(camera["measured"]["terminal"].values()) == camera["measured"]["decoded"]
        assert camera["event_latency_s"]["p95"] is None


def test_readiness_failure_preserves_failure_report(tmp_path):
    def empty(config):
        return iter(())
    with pytest.raises(RuntimeError, match="readiness"):
        run_batched_live(config(), DetectorConfig(model=Path("unused.pt")), tmp_path,
                         None, Sink(), source=empty, backend_factory=Backend,
                         tracker_factory=Tracker)
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert report["status"] == "failed"
    assert report["measurement_window"] is None
    assert report["unresolved_threads"] == []


def test_processing_failure_stops_run_and_keeps_exact_ledger(tmp_path):
    class BrokenTracker(Tracker):
        def update(self, item, result):
            raise ValueError("tracker failed")
    with pytest.raises(RuntimeError, match="failure"):
        run_batched_live(config(), DetectorConfig(model=Path("unused.pt")), tmp_path,
                         None, Sink(), source=source, backend_factory=Backend,
                         tracker_factory=BrokenTracker)
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert report["status"] == "failed"
    assert any("tracker failed" in c["errors"] for c in report["cameras"])
    for camera in report["cameras"]:
        assert sum(camera["terminal"].values()) == camera["frames_decoded"]


def test_bad_backend_correlations_fail_with_durable_report(tmp_path):
    class BrokenBackend(Backend):
        def detect_batch(self, batch):
            return ()
    with pytest.raises(RuntimeError, match="identities"):
        run_batched_live(config(), DetectorConfig(model=Path("unused.pt")), tmp_path,
                         None, Sink(), source=source, backend_factory=BrokenBackend,
                         tracker_factory=Tracker)
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert report["status"] == "failed"
    for camera in report["cameras"]:
        assert sum(camera["terminal"].values()) == camera["frames_decoded"]


def test_reconnect_fences_old_results_and_resets_camera_tracker(tmp_path):
    resets = []
    class RecordingTracker(Tracker):
        def reset(self, session):
            super().reset(session)
            resets.append(session)
    def reconnecting(config):
        index = 0
        while not config.stop_event.is_set():
            yield Frame(index, (index % 4) / 100., None, session_id=f"s{index // 4}")
            index += 1
            time.sleep(.01)
    run_batched_live(config(), DetectorConfig(model=Path("unused.pt")), tmp_path,
                     None, Sink(), source=reconnecting, backend_factory=Backend,
                     tracker_factory=RecordingTracker)
    assert "s0" in resets and "s1" in resets
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert all(sum(c["terminal"].values()) == c["frames_decoded"] for c in report["cameras"])


def test_database_health_failure_stops_all_cameras(tmp_path):
    class BrokenSink(Sink):
        def check_connection(self):
            raise ValueError("database unavailable")
    with pytest.raises(RuntimeError):
        run_batched_live(config(), DetectorConfig(model=Path("unused.pt")), tmp_path,
                         None, BrokenSink(), source=source, backend_factory=Backend,
                         tracker_factory=Tracker)
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert report["status"] == "failed"
    assert report["health_errors"] == ["database unavailable"]
    assert report["unresolved_threads"] == []
