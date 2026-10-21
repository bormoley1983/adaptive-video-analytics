"""PhaseTimer behavior checks (deterministic, no GPU).

The timer accumulates wall-clock seconds per named phase. With ``sync=False``
(CPU path) no CUDA synchronize happens, so the logic is fully deterministic and
testable without a device.
"""

from __future__ import annotations

import builtins
import sys
import time

import pytest

from ava.timing import (
    PhaseTimer,
    _cuda_sync,
    peak_gpu_memory_bytes,
    reset_peak_gpu_memory,
)


def _install_fake_torch(monkeypatch: pytest.MonkeyPatch, *, available: bool,
                        max_mem: int = 123456) -> dict:
    """Control the real torch.cuda methods so timing.py's CUDA branches run.

    The container has real torch with CUDA available. Rather than swapping the
    whole module (which the import machinery can cache around), we patch the
    individual ``torch.cuda`` callables that timing.py invokes and record calls.
    """
    import torch

    calls: dict[str, int] = {}

    def _is_available():
        return available

    def _synchronize():
        calls["synchronize"] = calls.get("synchronize", 0) + 1

    def _max_memory_allocated():
        return max_mem

    def _reset_peak_memory_stats():
        calls["reset"] = calls.get("reset", 0) + 1

    monkeypatch.setattr(torch.cuda, "is_available", _is_available)
    monkeypatch.setattr(torch.cuda, "synchronize", _synchronize)
    monkeypatch.setattr(torch.cuda, "max_memory_allocated", _max_memory_allocated)
    monkeypatch.setattr(torch.cuda, "reset_peak_memory_stats", _reset_peak_memory_stats)
    return calls


def test_total_accumulates_across_repeated_phases() -> None:
    timer = PhaseTimer(sync=False)
    for _ in range(3):
        timer.begin("decode")
        time.sleep(0.001)
        timer.end("decode")
    assert timer.total("decode") > 0.0
    # Three short sleeps should total well under a second.
    assert timer.total("decode") < 1.0


def test_end_returns_elapsed_for_active_phase() -> None:
    timer = PhaseTimer(sync=False)
    timer.begin("inference")
    time.sleep(0.002)
    elapsed = timer.end("inference")
    assert elapsed > 0.0
    # The returned interval matches what was accumulated for the phase.
    assert timer.total("inference") == elapsed


def test_end_with_explicit_name_overrides_active() -> None:
    timer = PhaseTimer(sync=False)
    timer.begin("decode")
    time.sleep(0.001)
    # Attribute the interval to a different phase name explicitly.
    timer.end("inference")
    assert timer.total("inference") > 0.0
    assert timer.total("decode") == 0.0


def test_unknown_phase_total_is_zero() -> None:
    timer = PhaseTimer(sync=False)
    assert timer.total("never-started") == 0.0


def test_percentile_nearest_rank() -> None:
    timer = PhaseTimer(sync=False)
    # Build a known sample set by timing trivial no-op phases.
    for _ in range(5):
        timer.begin("p")
        timer.end("p")
    samples = sorted(timer._samples["p"])
    assert len(samples) == 5
    # p50 of 5 samples is the middle (index round(0.5*4)=2).
    assert timer.percentile("p", 50) == samples[2]
    # p95 of 5 samples is index round(0.95*4)=4 (the max).
    assert timer.percentile("p", 95) == samples[4]


def test_percentile_empty_phase_is_zero() -> None:
    timer = PhaseTimer(sync=False)
    assert timer.percentile("none", 50) == 0.0


def test_mean_of_samples() -> None:
    timer = PhaseTimer(sync=False)
    for _ in range(4):
        timer.begin("m")
        time.sleep(0.001)
        timer.end("m")
    samples = timer._samples["m"]
    expected = sum(samples) / len(samples)
    assert timer.mean("m") == expected


def test_mean_empty_phase_is_zero() -> None:
    timer = PhaseTimer(sync=False)
    assert timer.mean("none") == 0.0


# --- CUDA-available branches (mocked torch, no real GPU) ---------------------


def test_cuda_sync_synchronizes_when_available(monkeypatch):
    calls = _install_fake_torch(monkeypatch, available=True)
    _cuda_sync()
    assert calls.get("synchronize") == 1


def test_cuda_sync_noop_when_unavailable(monkeypatch):
    calls = _install_fake_torch(monkeypatch, available=False)
    _cuda_sync()
    assert "synchronize" not in calls


def test_peak_gpu_memory_reads_cuda_value(monkeypatch):
    _install_fake_torch(monkeypatch, available=True, max_mem=987654)
    assert peak_gpu_memory_bytes() == 987654


def test_peak_gpu_memory_zero_when_unavailable(monkeypatch):
    _install_fake_torch(monkeypatch, available=False)
    assert peak_gpu_memory_bytes() == 0


def test_reset_peak_gpu_memory_calls_cuda(monkeypatch):
    calls = _install_fake_torch(monkeypatch, available=True)
    reset_peak_gpu_memory()
    assert calls.get("reset") == 1


def test_phase_timer_sync_triggers_cuda_sync(monkeypatch):
    calls = _install_fake_torch(monkeypatch, available=True)
    timer = PhaseTimer(sync=True)
    timer.begin("decode")
    timer.end("decode")
    # begin() synchronizes once when sync is enabled (end() does not).
    assert calls.get("synchronize") == 1


# --- torch-not-installed fallback (ImportError) ------------------------------


def _block_torch_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import torch`` raise ImportError to exercise the CPU fallback."""

    def _raise(*_a, **_kw):
        raise ImportError("torch not installed")

    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(builtins, "__import__", _raise, raising=False)


def test_cuda_sync_import_error_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_torch_import(monkeypatch)
    # Must not raise; the ImportError is swallowed.
    _cuda_sync()


def test_peak_gpu_memory_import_error_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_torch_import(monkeypatch)
    assert peak_gpu_memory_bytes() == 0


def test_reset_peak_gpu_memory_import_error_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_torch_import(monkeypatch)
    # Must not raise; the ImportError is swallowed.
    reset_peak_gpu_memory()
