"""Per-camera and host metric collection for live runs (E-3-T4).

Collects:
- Per-camera: input/analyzed FPS, queue depth/age, drops by reason,
  inference timing (p50/p95), event latency (p50/p95).
- Host: CPU%, RAM, VRAM, GPU compute/decoder utilization (via nvidia-smi).

Output is a single ``metrics.json`` written at run end. Time-series samples
are embedded as arrays so the file stays self-contained without requiring
a separate JSONL stream (events are sparse in production; periodic video
frames do not exist between rule firings).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Compute the pct-th percentile (0-100) of a sorted list.

    Uses linear interpolation between closest ranks (same as numpy's
    default 'linear' method).
    """
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    # Rank: 0-indexed position with fractional part.
    rank = (pct / 100.0) * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    frac = rank - lower
    return sorted_data[lower] + (sorted_data[upper] - sorted_data[lower]) * frac


# ---------------------------------------------------------------------------
# Per-camera metric accumulators
# ---------------------------------------------------------------------------


@dataclass
class InferenceTiming:
    """Tracks inference call durations for percentile computation."""

    _durations: list[float] = field(default_factory=list)

    def record(self, seconds: float) -> None:
        self._durations.append(seconds)

    def percentiles(self) -> dict[str, float]:
        if not self._durations:
            return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}
        sorted_d = sorted(self._durations)
        n = len(sorted_d)
        p50 = _percentile(sorted_d, 50)
        p95 = _percentile(sorted_d, 95)
        mean = sum(sorted_d) / n
        return {"p50": round(p50, 6), "p95": round(p95, 6), "mean": round(mean, 6), "count": n}


@dataclass
class EventLatency:
    """Tracks source-PTS-to-event-emission latency in seconds."""

    _latencies: list[float] = field(default_factory=list)

    def record(self, seconds: float) -> None:
        self._latencies.append(seconds)

    def percentiles(self) -> dict[str, float]:
        if not self._latencies:
            return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}
        sorted_l = sorted(self._latencies)
        n = len(sorted_l)
        p50 = _percentile(sorted_l, 50)
        p95 = _percentile(sorted_l, 95)
        mean = sum(sorted_l) / n
        return {"p50": round(p50, 6), "p95": round(p95, 6), "mean": round(mean, 6), "count": n}


@dataclass
class CameraMetrics:
    """Accumulates metrics for one camera over a live run."""

    camera_id: str
    frames_decoded: int = 0
    frames_inferred: int = 0
    frames_dropped_buffer: int = 0
    frames_dropped_reconnect: int = 0
    frames_dropped_error: int = 0
    events_emitted: int = 0
    reconnects: int = 0
    errors: list[str] = field(default_factory=list)
    inference: InferenceTiming = field(default_factory=InferenceTiming)
    event_latency: EventLatency = field(default_factory=EventLatency)
    # Queue state samples (taken periodically by the sampler thread).
    queue_depth_samples: list[int] = field(default_factory=list)
    queue_age_samples: list[float] = field(default_factory=list)
    # Wall-clock start for FPS computation.
    _start_time: float = field(default_factory=time.monotonic, repr=False)

    def record_decode(self) -> None:
        self.frames_decoded += 1

    def record_inference(self, duration_s: float) -> None:
        self.frames_inferred += 1
        self.inference.record(duration_s)

    def record_drop_buffer(self) -> None:
        self.frames_dropped_buffer += 1

    def record_drop_reconnect(self) -> None:
        self.frames_dropped_reconnect += 1

    def record_drop_error(self) -> None:
        self.frames_dropped_error += 1

    def record_event(self, latency_s: float) -> None:
        self.events_emitted += 1
        self.event_latency.record(latency_s)

    def record_reconnect(self) -> None:
        self.reconnects += 1

    def record_error(self, msg: str) -> None:
        self.errors.append(msg)

    def sample_queue(self, depth: int, age_s: float) -> None:
        self.queue_depth_samples.append(depth)
        self.queue_age_samples.append(round(age_s, 4))

    @property
    def wall_seconds(self) -> float:
        return time.monotonic() - self._start_time

    @property
    def input_fps(self) -> float:
        w = self.wall_seconds
        return round(self.frames_decoded / w, 3) if w > 0 else 0.0

    @property
    def analyzed_fps(self) -> float:
        w = self.wall_seconds
        return round(self.frames_inferred / w, 3) if w > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "wall_seconds": round(self.wall_seconds, 3),
            "frames_decoded": self.frames_decoded,
            "frames_inferred": self.frames_inferred,
            "frames_dropped": {
                "buffer_full": self.frames_dropped_buffer,
                "reconnect": self.frames_dropped_reconnect,
                "error": self.frames_dropped_error,
            },
            "input_fps": self.input_fps,
            "analyzed_fps": self.analyzed_fps,
            "events_emitted": self.events_emitted,
            "reconnects": self.reconnects,
            "errors": list(self.errors),
            "inference_timing": self.inference.percentiles(),
            "event_latency": self.event_latency.percentiles(),
            "queue_depth_samples": self.queue_depth_samples,
            "queue_age_samples": self.queue_age_samples,
        }


# ---------------------------------------------------------------------------
# Host / GPU resource sampler
# ---------------------------------------------------------------------------


@dataclass
class GpuSample:
    """One nvidia-smi snapshot."""

    timestamp: float
    gpu_util_pct: float
    mem_used_mib: int
    mem_total_mib: int
    decoder_util_pct: float | None = None  # may be unavailable


def _query_gpu(gpu_index: str = "0") -> GpuSample | None:
    """Query nvidia-smi for one GPU. Returns None if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--id={gpu_index}",
                "--query-gpu=utilization.gpu,memory.used,memory.total,utilization.decoder",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        parts = [p.strip() for p in result.stdout.split(",")]
        gpu_util = float(parts[0])
        mem_used = int(parts[1])
        mem_total = int(parts[2])
        decoder_util = float(parts[3]) if len(parts) > 3 and parts[3] else None
        return GpuSample(
            timestamp=time.time(),
            gpu_util_pct=gpu_util,
            mem_used_mib=mem_used,
            mem_total_mib=mem_total,
            decoder_util_pct=decoder_util,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        return None


def _query_host() -> dict:
    """Query basic host CPU/RAM. Uses /proc to avoid psutil dependency."""
    info: dict = {}
    # CPU usage from /proc/stat (delta over a short interval is complex;
    # use load average as a simple proxy).
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            info["load_1m"] = float(parts[0])
            info["load_5m"] = float(parts[1])
            info["load_15m"] = float(parts[2])
    except (OSError, IndexError):
        pass
    # RAM from /proc/meminfo.
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, val = line.partition(":")
                meminfo[key.strip()] = int(val.strip().split()[0])  # kB
        info["ram_total_mib"] = meminfo.get("MemTotal", 0) // 1024
        info["ram_available_mib"] = meminfo.get("MemAvailable", 0) // 1024
    except (OSError, ValueError):
        pass
    return info


# ---------------------------------------------------------------------------
# Run-level metrics aggregator
# ---------------------------------------------------------------------------


class LiveMetricsCollector:
    """Aggregates per-camera and host metrics for one live run.

    Usage:
        collector = LiveMetricsCollector(gpu_index="0")
        cam_metrics = collector.register_camera("cam-01")
        # ... during run: cam_metrics.record_decode(), etc.
        # ... periodically: collector.sample_host()
        # at end: path = collector.write(output_dir / "metrics.json")
    """

    def __init__(self, gpu_index: str = "0", sample_interval_s: float = 5.0) -> None:
        self.gpu_index = gpu_index
        self.sample_interval_s = sample_interval_s
        self._cameras: dict[str, CameraMetrics] = {}
        self._host_samples: list[dict] = []
        self._gpu_samples: list[GpuSample] = []
        self._start_time = time.monotonic()

    def register_camera(self, camera_id: str) -> CameraMetrics:
        cm = CameraMetrics(camera_id=camera_id)
        self._cameras[camera_id] = cm
        return cm

    def get_camera(self, camera_id: str) -> CameraMetrics | None:
        return self._cameras.get(camera_id)

    def sample_host(self) -> None:
        """Take one host + GPU sample. Call periodically (e.g., every 5 s)."""
        host = _query_host()
        gpu = _query_gpu(self.gpu_index)
        entry: dict = {
            "t": round(time.monotonic() - self._start_time, 3),
            **host,
        }
        if gpu is not None:
            entry["gpu_util_pct"] = gpu.gpu_util_pct
            entry["gpu_mem_used_mib"] = gpu.mem_used_mib
            entry["gpu_mem_total_mib"] = gpu.mem_total_mib
            if gpu.decoder_util_pct is not None:
                entry["gpu_decoder_util_pct"] = gpu.decoder_util_pct
        else:
            entry["gpu_available"] = False
        self._host_samples.append(entry)

    @property
    def wall_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def to_dict(self) -> dict:
        return {
            "wall_seconds": round(self.wall_seconds, 3),
            "gpu_index": self.gpu_index,
            "cameras": [cm.to_dict() for cm in self._cameras.values()],
            "host_samples": self._host_samples,
        }

    def write(self, path: Path) -> Path:
        """Serialize metrics to a JSON file and return the path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        return path
