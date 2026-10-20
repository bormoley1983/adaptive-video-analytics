"""CUDA-synchronized per-phase timing for baseline measurement.

The runner decodes, infers, and writes outputs in a single loop. To report an
honest baseline (decode vs analysis vs export, warm-up vs steady-state, p50/p95
latency) we time each phase with ``time.perf_counter`` and, when running on
CUDA, synchronize the device before reading the clock so GPU work is not counted
in the *next* phase. Synchronizing once per frame is the correct way to attribute
GPU inference cost; it adds a small fixed overhead that is identical across runs
and therefore does not distort relative comparisons.
"""

from __future__ import annotations

import statistics
import time


def _cuda_sync() -> None:
    """Synchronize the CUDA device if torch/CUDA is available, else no-op."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
    except Exception:  # pragma: no cover - CPU smoke path
        pass


class PhaseTimer:
    """Accumulate wall-clock seconds for named phases with CUDA sync.

    Call ``begin(name)`` then ``end(name)`` around each phase. When ``sync`` is
    True (GPU runs), a device synchronize happens at the end of each phase so the
    measured interval includes the GPU work that phase launched.
    """

    def __init__(self, sync: bool = False) -> None:
        self.sync = sync
        self._totals: dict[str, float] = {}
        self._samples: dict[str, list[float]] = {}
        self._active: str | None = None
        self._start = 0.0

    def begin(self, name: str) -> None:
        if self.sync:
            _cuda_sync()
        self._active = name
        self._start = time.perf_counter()

    def end(self, name: str | None = None) -> float:
        assert self._active is not None, "begin() before end()"
        elapsed = time.perf_counter() - self._start
        key = name or self._active
        self._totals[key] = self._totals.get(key, 0.0) + elapsed
        self._samples.setdefault(key, []).append(elapsed)
        self._active = None
        return elapsed

    def total(self, name: str) -> float:
        return self._totals.get(name, 0.0)

    def percentile(self, name: str, pct: float) -> float:
        """Nearest-rank percentile of the per-frame samples for a phase."""
        samples = sorted(self._samples.get(name, []))
        if not samples:
            return 0.0
        rank = int(round((pct / 100.0) * (len(samples) - 1)))
        return samples[rank]

    def mean(self, name: str) -> float:
        samples = self._samples.get(name, [])
        return statistics.fmean(samples) if samples else 0.0


def peak_gpu_memory_bytes() -> int:
    """Return peak CUDA memory allocated by this process (0 on CPU)."""
    try:
        import torch

        if torch.cuda.is_available():
            return int(torch.cuda.max_memory_allocated())
    except Exception:  # pragma: no cover - CPU smoke path
        pass
    return 0


def reset_peak_gpu_memory() -> None:
    """Reset the CUDA peak-memory high-water mark before a timed run."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:  # pragma: no cover - CPU smoke path
        pass
