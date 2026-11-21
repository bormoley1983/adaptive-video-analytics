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
    def __init__(self, *, max_samples=1024, max_series=512, sample_every=1, clock=time.monotonic,
                 device_profile=False):
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
        # Device-interval profiling is a separate opt-in with its own batch
        # selection policy: exactly one batch per device_sample_every calls.
        self.device_profile = bool(device_profile)
        self.device_sample_every = sample_every
        self.device_stream = None
        self._device_calls = 0
        self._device_samples = {}
        self._device_dropped = 0
        self._device_batch_count = 0
        self._device_totals = {}

    def select_device_batch(self):
        """Return the selected batch's one-based ordinal, or None.

        The ordinal is assigned at this call, so unselected batches perform no
        measurement event operations and no synchronizations downstream.
        """
        if not self.device_profile:
            return None
        with self._lock:
            self._device_calls += 1
            if (self._device_calls - 1) % self.device_sample_every == 0:
                return self._device_calls
            return None

    def record_device_batch(self, *, batch_id, size, camera_keys, ordinal, stream,
                            h2d_interval_s=None, inference_interval_s=None,
                            sync_host_wall_s=None, status="available",
                            host_started_at=None, host_finished_at=None):
        """Store one selected batch's paired device intervals and sync cost.

        ``host_started_at`` / ``host_finished_at`` are the pair of monotonic
        timestamps bracketing the whole prediction call for this batch; the
        window filter uses them (the window is in run-time seconds, not batch
        ordinals). Selection happened at selection time, before any event
        operation or synchronization.
        """
        if not self.device_profile:
            return
        with self._lock:
            self.device_stream = stream
            entry = {"batch_id": batch_id, "size": size, "camera_keys": list(camera_keys),
                     "ordinal": ordinal, "stream": stream, "status": status}
            for field_name, value in (("h2d_interval_s", h2d_interval_s),
                                      ("inference_interval_s", inference_interval_s),
                                      ("sync_host_wall_s", sync_host_wall_s)):
                entry[field_name] = None if value is None or not isfinite(value) or value < 0 else value
            host_valid = (host_started_at is not None and host_finished_at is not None
                          and all(isfinite(v) for v in (host_started_at, host_finished_at))
                          and host_finished_at >= host_started_at)
            entry["host_started_at"] = host_started_at if host_valid else None
            entry["host_finished_at"] = host_finished_at if host_valid else None
            if self.window is not None and (
                not host_valid or not (self.window[0] <= host_started_at and host_finished_at < self.window[1])
            ):
                return
            self._device_batch_count += 1
            if status == "available":
                for field_name in ("h2d_interval_s", "inference_interval_s", "sync_host_wall_s"):
                    value = entry[field_name]
                    if value is not None:
                        aggregate = self._device_totals.setdefault(field_name, {"count": 0, "total_s": 0.})
                        aggregate["count"] += 1
                        aggregate["total_s"] += value
            if len(self._device_samples) == self.max_samples:
                del self._device_samples[min(self._device_samples)]
                self._device_dropped += 1
            self._device_samples[ordinal] = entry

    @staticmethod
    def _percentile(samples, fraction):
        ordered = sorted(samples)
        return ordered[int((len(ordered) - 1) * fraction)] if ordered else None

    def _device_series(self, samples, field_name):
        values = [s[field_name] for s in samples
                  if s.get(field_name) is not None and s.get("status") == "available"]
        aggregate = self._device_totals.get(field_name)
        if aggregate is None:
            return None
        return {**aggregate, "sample_count": len(values), "truncated": aggregate["count"] > len(values),
                "p50_s": self._percentile(values, .5), "p95_s": self._percentile(values, .95)}

    def _device_section(self):
        with self._lock:
            samples = [self._device_samples[key] for key in sorted(self._device_samples)]
            return {"enabled": self.device_profile,
                    "sample_policy": "deterministic batch selection before event operations; every "
                                     f"{self.device_sample_every}th batch",
                    "batches_seen_all_phases": self._device_calls,
                    "batches_selected": self._device_batch_count,
                    "samples_truncated": self._device_dropped > 0,
                    "stream": self.device_stream,
                    "interval_scope": "Both h2d_device_interval_s and inference_device_interval_s "
                                      "cover the same whole prediction call (preprocess + inference + "
                                      "postprocessing) on the declared stream; they are not separately "
                                      "attributable H2D or kernel-only durations",
                    "excluded_samples": "warm-up, drain and boundary-crossing spans are not recorded",
                    "h2d_device_interval_s": self._device_series(samples, "h2d_interval_s"),
                    "inference_device_interval_s": self._device_series(samples, "inference_interval_s"),
                    "sync_host_wall_s": self._device_series(samples, "sync_host_wall_s"),
                    "samples": samples}

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
            report = {"clock": "monotonic_host_wall",
                      "window": self.window if self.window is None or all(isfinite(v) for v in self.window) else None,
                      "sample_policy": "deterministic per stage/camera; counts/totals describe selected complete-window spans only",
                      "calls_include_warmup_and_drain": True,
                      "default_sample_every": self.sample_every,
                      "invalid_samples": self.invalid_samples, "dropped_series": self.dropped_series,
                      "device_kernel_s": None, "isolated_h2d_s": None, "isolated_d2h_s": None,
                      "device_timing_status": "unavailable; predictor spans are host wall time, not kernel time",
                      "series": series}
        if self.device_profile:
            report["device_profile"] = self._device_section()
        return report


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
