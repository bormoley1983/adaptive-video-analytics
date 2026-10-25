"""Unit tests for live metric collection (E-3-T4)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ava.live_metrics import (
    CameraMetrics,
    EventLatency,
    InferenceTiming,
    LiveMetricsCollector,
    _query_gpu,
    _query_host,
)


class TestInferenceTiming:
    def test_empty_percentiles(self) -> None:
        t = InferenceTiming()
        result = t.percentiles()
        assert result == {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}

    def test_single_sample(self) -> None:
        t = InferenceTiming()
        t.record(0.1)
        result = t.percentiles()
        assert result["count"] == 1
        assert result["p50"] == pytest.approx(0.1)
        assert result["p95"] == pytest.approx(0.1)
        assert result["mean"] == pytest.approx(0.1)

    def test_multiple_samples_percentiles(self) -> None:
        t = InferenceTiming()
        for v in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            t.record(v)
        result = t.percentiles()
        assert result["count"] == 10
        # Linear interpolation: p50 at rank 4.5 -> (0.5+0.6)/2 = 0.55
        assert result["p50"] == pytest.approx(0.55)
        # p95 at rank 8.55 -> 0.9 + 0.55*(1.0-0.9) = 0.955
        assert result["p95"] == pytest.approx(0.955)
        assert result["mean"] == pytest.approx(0.55)


class TestEventLatency:
    def test_empty(self) -> None:
        e = EventLatency()
        assert e.percentiles()["count"] == 0

    def test_records_and_computes(self) -> None:
        e = EventLatency()
        for v in [1.0, 2.0, 3.0]:
            e.record(v)
        result = e.percentiles()
        assert result["count"] == 3
        assert result["p50"] == pytest.approx(2.0)
        assert result["mean"] == pytest.approx(2.0)


class TestCameraMetrics:
    def test_initial_state(self) -> None:
        cm = CameraMetrics(camera_id="cam-01")
        assert cm.camera_id == "cam-01"
        assert cm.frames_decoded == 0
        assert cm.frames_inferred == 0
        assert cm.events_emitted == 0

    def test_record_decode(self) -> None:
        cm = CameraMetrics(camera_id="c")
        cm.record_decode()
        cm.record_decode()
        assert cm.frames_decoded == 2

    def test_measurement_window_starts_on_first_decode_and_stops_on_finish(
        self, monkeypatch
    ) -> None:
        clock = iter([10.0, 20.0, 30.0])
        monkeypatch.setattr("ava.live_metrics.time.monotonic", lambda: next(clock))
        cm = CameraMetrics(camera_id="c")

        cm.record_decode()
        cm.finish()

        assert cm.measurement_seconds == pytest.approx(10.0)
        assert cm.input_fps == pytest.approx(0.1)

    def test_record_inference(self) -> None:
        cm = CameraMetrics(camera_id="c")
        cm.record_inference(0.05)
        cm.record_inference(0.10)
        assert cm.frames_inferred == 2
        assert cm.inference.percentiles()["count"] == 2

    def test_record_drops(self) -> None:
        cm = CameraMetrics(camera_id="c")
        cm.record_drop_buffer()
        cm.record_drop_reconnect()
        cm.record_drop_error()
        assert cm.frames_dropped_buffer == 1
        assert cm.frames_dropped_reconnect == 1
        assert cm.frames_dropped_error == 1

    def test_record_event(self) -> None:
        cm = CameraMetrics(camera_id="c")
        cm.record_event(0.5)
        assert cm.events_emitted == 1
        assert cm.event_latency.percentiles()["count"] == 1

    def test_record_reconnect_and_error(self) -> None:
        cm = CameraMetrics(camera_id="c")
        cm.record_reconnect()
        cm.record_error("timeout")
        assert cm.reconnects == 1
        assert cm.errors == ["timeout"]

    def test_sample_queue(self) -> None:
        cm = CameraMetrics(camera_id="c")
        cm.sample_queue(5, 0.25)
        cm.sample_queue(10, 0.5)
        assert cm.queue_depth_samples == [5, 10]
        assert cm.queue_age_samples == [0.25, 0.5]

    def test_fps_properties(self) -> None:
        cm = CameraMetrics(camera_id="c")
        # wall_seconds is > 0 after construction (monotonic clock).
        cm.record_decode()
        cm.record_inference(0.01)
        assert cm.input_fps >= 0.0
        assert cm.analyzed_fps >= 0.0

    def test_to_dict_structure(self) -> None:
        cm = CameraMetrics(camera_id="cam-01")
        cm.record_decode()
        cm.record_inference(0.05)
        cm.record_event(0.1)
        d = cm.to_dict()
        assert d["camera_id"] == "cam-01"
        assert d["frames_decoded"] == 1
        assert d["frames_inferred"] == 1
        assert "measurement_seconds" in d
        assert d["events_emitted"] == 1
        assert "inference_timing" in d
        assert "event_latency" in d
        assert "queue_depth_samples" in d
        assert d["frames_dropped"]["buffer_full"] == 0


class TestQueryGpu:
    @patch("ava.live_metrics.subprocess.run")
    def test_success(self, mock_run: pytest.Mock) -> None:
        mock_run.return_value = type(
            "R", (), {"returncode": 0, "stdout": "45, 1024, 24576, 12\n"}
        )()
        sample = _query_gpu("0")
        assert sample is not None
        assert sample.gpu_util_pct == 45.0
        assert sample.mem_used_mib == 1024
        assert sample.mem_total_mib == 24576
        assert sample.decoder_util_pct == 12.0

    @patch("ava.live_metrics.subprocess.run")
    def test_nonzero_exit(self, mock_run: pytest.Mock) -> None:
        mock_run.return_value = type(
            "R", (), {"returncode": 1, "stdout": ""}
        )()
        assert _query_gpu("0") is None

    @patch("ava.live_metrics.subprocess.run")
    def test_timeout(self, mock_run: pytest.Mock) -> None:
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired(cmd="nvidia-smi", timeout=5)
        assert _query_gpu("0") is None

    @patch("ava.live_metrics.subprocess.run")
    def test_no_decoder_field(self, mock_run: pytest.Mock) -> None:
        mock_run.return_value = type(
            "R", (), {"returncode": 0, "stdout": "30, 512, 8192\n"}
        )()
        sample = _query_gpu("0")
        assert sample is not None
        assert sample.decoder_util_pct is None


class TestQueryHost:
    def test_returns_dict(self) -> None:
        info = _query_host()
        assert isinstance(info, dict)
        # On Linux, loadavg and meminfo should be available.
        if "load_1m" in info:
            assert info["load_1m"] >= 0.0
        if "ram_total_mib" in info:
            assert info["ram_total_mib"] > 0


class TestLiveMetricsCollector:
    def test_register_and_get_camera(self) -> None:
        c = LiveMetricsCollector()
        cm = c.register_camera("cam-01")
        assert c.get_camera("cam-01") is cm
        assert c.get_camera("nonexistent") is None

    def test_sample_host(self) -> None:
        c = LiveMetricsCollector()
        c.sample_host()
        assert len(c._host_samples) == 1
        entry = c._host_samples[0]
        assert "t" in entry

    def test_to_dict_structure(self) -> None:
        c = LiveMetricsCollector(gpu_index="0")
        cm = c.register_camera("cam-01")
        cm.record_decode()
        cm.record_inference(0.05)
        c.sample_host()
        d = c.to_dict()
        assert d["gpu_index"] == "0"
        assert len(d["cameras"]) == 1
        assert d["cameras"][0]["camera_id"] == "cam-01"
        assert len(d["host_samples"]) == 1

    def test_write_json(self, tmp_path: Path) -> None:
        c = LiveMetricsCollector()
        cm = c.register_camera("cam-01")
        cm.record_decode()
        path = c.write(tmp_path / "metrics.json")
        assert path.exists()
        with path.open() as f:
            data = json.load(f)
        assert data["cameras"][0]["camera_id"] == "cam-01"

    def test_wall_seconds_positive(self) -> None:
        c = LiveMetricsCollector()
        assert c.wall_seconds >= 0.0
