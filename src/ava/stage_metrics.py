"""Bounded opt-in host timing and CPU-delta measurements."""

import os
import resource
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from math import isfinite
from pathlib import Path
from threading import Lock
from threading import enumerate as enumerate_threads


class StageRecorder:
    def __init__(self, *, max_samples=1024, max_series=512, sample_every=1, clock=time.monotonic):
        if max_samples < 1 or max_series < 1 or sample_every < 1:
            raise ValueError("sample and series limits must be positive")
        self.sample_every = sample_every
        self._calls = {}
        self.max_samples = max_samples
        self.max_series = max_series
        self.clock = clock
        self.window = None
        self.invalid_samples = 0
        self.dropped_series = 0
        self._series = {}
        self._lock = Lock()

    def _select(self, stage, camera):
        key = (stage, camera)
        with self._lock:
            if key not in self._calls and len(self._calls) == self.max_series:
                self.dropped_series += 1
                return False
            calls = self._calls.get(key, 0) + 1
            self._calls[key] = calls
            interval = self._interval(stage)
            return (calls - 1) % interval == 0

    def _interval(self, stage):
        return 1 if stage.startswith("sink_") else self.sample_every

    def observe(self, stage, started, finished, camera=None):
        if self._select(stage, camera):
            self._observe_sample(stage, started, finished, camera)

    def _observe_sample(self, stage, started, finished, camera=None):
        with self._lock:
            try:
                valid = all(isfinite(value) for value in (started, finished)) and finished >= started
            except TypeError:
                valid = False
            if not valid:
                self.invalid_samples += 1
                return
            if self.window is not None and not (self.window[0] <= started and finished < self.window[1]):
                return
            key = (stage, camera)
            if key not in self._series:
                if len(self._series) == self.max_series:
                    self.dropped_series += 1
                    return
                self._series[key] = {"count": 0, "total_s": 0., "min_s": float("inf"),
                                     "max_s": 0., "samples": deque(maxlen=self.max_samples)}
            entry = self._series[key]
            seconds = finished - started
            entry["count"] += 1
            entry["total_s"] += seconds
            entry["min_s"] = min(entry["min_s"], seconds)
            entry["max_s"] = max(entry["max_s"], seconds)
            entry["samples"].append(seconds)

    @contextmanager
    def measure(self, stage, camera=None):
        if not self._select(stage, camera):
            yield
            return
        def read_clock():
            try:
                return self.clock()
            except (OSError, ValueError, TypeError, RuntimeError):
                return float("nan")
        started = read_clock()
        try:
            yield
        finally:
            self._observe_sample(stage, started, read_clock(), camera)

    def report(self):
        with self._lock:
            series = []
            for (stage, camera), values in sorted(self._series.items(), key=lambda pair: str(pair[0])):
                entry = {key: value for key, value in values.items() if key != "samples"}
                samples = sorted(values["samples"])
                series.append({"stage": stage, "camera_id": camera, **entry,
                               "sample_count": len(samples), "truncated": values["count"] > len(samples),
                               "sample_every": self._interval(stage),
                               "calls_all_phases": self._calls[(stage, camera)],
                               "p50_s": samples[int((len(samples) - 1) * .5)],
                               "p95_s": samples[int((len(samples) - 1) * .95)]})
            return {"clock": "monotonic_host_wall",
                    "window": self.window if self.window is None or all(isfinite(v) for v in self.window) else None,
                    "sample_policy": "deterministic per stage/camera; counts/totals describe selected complete-window spans only",
                    "calls_include_warmup_and_drain": True,
                    "default_sample_every": self.sample_every,
                    "invalid_samples": self.invalid_samples, "dropped_series": self.dropped_series,
                    "device_kernel_s": None, "isolated_h2d_s": None, "isolated_d2h_s": None,
                    "device_timing_status": "unavailable; predictor spans are host wall time, not kernel time",
                    "series": series}


def stage_span(recorder, stage, camera=None):
    return recorder.measure(stage, camera) if recorder is not None else nullcontext()


class CpuSampler:
    """Process/thread percentages use one full core as 100%; host uses all cores."""

    def __init__(self, *, proc=Path("/proc"), clock=time.monotonic,
                 process_time=time.process_time, usage=resource.getrusage, minimum_interval_s=0.):
        if not isfinite(minimum_interval_s) or minimum_interval_s < 0:
            raise ValueError("CPU sampling interval must be finite and non-negative")
        self.minimum_interval_s = minimum_interval_s
        self.proc = proc
        self.clock = clock
        self.process_time = process_time
        self.usage = usage
        self.ticks = os.sysconf("SC_CLK_TCK")
        self.previous = None

    def sample(self):
        try:
            now = self.clock()
            if self.previous is not None and 0 <= now - self.previous[0] < self.minimum_interval_s:
                return {"process_cpu_pct": None, "thread_cpu_pct": {}, "host_cpu_busy_pct": None,
                        "host_iowait_pct": None, "unavailable": ["cpu_sample_not_scheduled"]}
            return self._sample(now)
        except (OSError, ValueError, IndexError):
            self.previous = None
            return {"process_cpu_pct": None, "thread_cpu_pct": {}, "host_cpu_busy_pct": None,
                    "host_iowait_pct": None, "unavailable": ["cpu_sampler_failed"]}

    def _sample(self, now):
        cpu = self.process_time()
        usage = self.usage(resource.RUSAGE_SELF)
        threads = {}
        unavailable = []
        try:
            for path in (self.proc / "self/task").glob("*/stat"):
                try:
                    raw = path.read_text()
                    fields = raw[raw.rfind(")") + 2:].split()
                    threads[path.parent.name] = (fields[19], (int(fields[11]) + int(fields[12])) / self.ticks)
                except (OSError, ValueError, IndexError):
                    unavailable.append("thread:" + path.parent.name)
        except OSError:
            unavailable.append("threads")
        host = None
        try:
            rows = (self.proc / "stat").read_text().splitlines()
            fields = rows[0].split()
            values = [int(v) for v in fields[1:9]]
            if fields[0] != "cpu" or len(values) != 8:
                raise ValueError("invalid aggregate CPU row")
            topology = tuple(row.split()[0] for row in rows[1:]
                             if row.startswith("cpu") and row.split()[0][3:].isdigit())
            host = (tuple(values), topology)
        except (OSError, ValueError, IndexError):
            unavailable.append("host_cpu")
        current = (now, cpu, usage.ru_nvcsw, usage.ru_nivcsw, threads, host)
        previous, self.previous = self.previous, current
        result = {"process_cpu_pct": None, "thread_cpu_pct": {},
                  "thread_names": {str(t.native_id): t.name for t in enumerate_threads() if t.native_id is not None},
                  "host_cpu_busy_pct": None, "host_iowait_pct": None, "voluntary_context_switches": None,
                  "involuntary_context_switches": None, "unavailable": unavailable,
                  "cpu_percent_basis": "100% per core for process/threads; whole host for host CPU"}
        if (previous is None or not all(isfinite(v) for v in (now, cpu, previous[0], previous[1]))
                or now <= previous[0] or cpu < previous[1]):
            return result
        elapsed = now - previous[0]
        result["interval_started_at"] = previous[0]
        result["interval_finished_at"] = now
        result["process_cpu_pct"] = 100 * (cpu - previous[1]) / elapsed
        for name, (identity, seconds) in threads.items():
            old = previous[4].get(name)
            if old is not None and old[0] == identity and seconds >= old[1]:
                result["thread_cpu_pct"][name] = 100 * (seconds - old[1]) / elapsed
        for name, value, old in (("voluntary_context_switches", current[2], previous[2]),
                                 ("involuntary_context_switches", current[3], previous[3])):
            if value >= old:
                result[name] = value - old
        if host is not None and previous[5] is not None:
            delta = tuple(a - b for a, b in zip(host[0], previous[5][0], strict=True))
            total = sum(delta)
            if host[1] == previous[5][1] and total > 0 and min(delta) >= 0:
                result["host_cpu_busy_pct"] = 100 * (total - delta[3] - delta[4]) / total
                result["host_iowait_pct"] = 100 * delta[4] / total
        return result
