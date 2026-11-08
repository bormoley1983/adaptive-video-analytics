from types import SimpleNamespace

import pytest

from ava.stage_metrics import CpuSampler, StageRecorder, stage_span


def test_bounded_stage_samples_keep_total_counts_and_window_boundaries():
    recorder = StageRecorder(max_samples=2, max_series=1)
    recorder.window = (1., 5.)
    for start, end in [(0., 2.), (1., 2.), (2., 3.), (3., 4.), (4., 5.)]:
        recorder.observe("decode", start, end, "a")
    recorder.observe("other", 2., 3.)
    report = recorder.report()
    series = report["series"][0]
    assert series["count"] == 3
    assert series["sample_count"] == 2 and series["truncated"]
    assert series["total_s"] == series["max_s"] * 3 == 3.
    assert report["dropped_series"] == 1
    assert report["device_kernel_s"] is None


@pytest.mark.parametrize("start,end", [(2., 1.), (float("nan"), 2.), (1., float("inf")), (None, 1.)])
def test_invalid_stage_clocks_do_not_emit_measurements(start, end):
    recorder = StageRecorder()
    recorder.observe("decode", start, end)
    assert recorder.report()["series"] == []
    assert recorder.report()["invalid_samples"] == 1


@pytest.mark.parametrize("samples,series", [(0, 1), (1, 0)])
def test_rejects_unbounded_or_empty_recorder_limits(samples, series):
    with pytest.raises(ValueError):
        StageRecorder(max_samples=samples, max_series=series)


def test_failed_stage_preserves_exception_and_records_elapsed_time():
    ticks = iter([1., 1.25])
    recorder = StageRecorder(clock=lambda: next(ticks))
    with pytest.raises(ValueError, match="original"), recorder.measure("backend"):
        raise ValueError("original")
    assert recorder.report()["series"][0]["total_s"] == .25
    with stage_span(None, "disabled"):
        pass


def test_clock_error_cannot_mask_original_stage_failure():
    def broken_clock():
        raise OSError("clock unavailable")
    recorder = StageRecorder(clock=broken_clock)
    with pytest.raises(RuntimeError, match="backend failed"), recorder.measure("backend"):
        raise RuntimeError("backend failed")
    assert recorder.report()["invalid_samples"] == 1


def write_cpu_snapshot(root, *, user, idle, iowait, ticks, identity="100"):
    root.mkdir(exist_ok=True)
    (root / "stat").write_text(f"cpu {user} 0 0 {idle} {iowait} 0 0 0 999 999\n")
    path = root / "self/task/12"
    path.mkdir(parents=True, exist_ok=True)
    fields = ["0"] * 20
    fields[0], fields[11], fields[12], fields[19] = "S", str(ticks), "0", identity
    (path / "stat").write_text("12 (worker (nested) name) " + " ".join(fields))


def test_cpu_deltas_separate_host_thread_and_process_with_guest_excluded(tmp_path):
    clock = iter([10., 11., 12.])
    cpu = iter([1., 3., 4.])
    switches = iter([SimpleNamespace(ru_nvcsw=1, ru_nivcsw=2),
                     SimpleNamespace(ru_nvcsw=4, ru_nivcsw=6),
                     SimpleNamespace(ru_nvcsw=5, ru_nivcsw=7)])
    sampler = CpuSampler(proc=tmp_path, clock=lambda: next(clock),
                         process_time=lambda: next(cpu), usage=lambda _: next(switches))
    write_cpu_snapshot(tmp_path, user=100, idle=100, iowait=10, ticks=10)
    assert sampler.sample()["process_cpu_pct"] is None
    write_cpu_snapshot(tmp_path, user=160, idle=120, iowait=30, ticks=10 + sampler.ticks)
    sample = sampler.sample()
    assert sample["process_cpu_pct"] == 200.
    assert sample["thread_cpu_pct"] == {"12": 100.}
    assert sample["host_cpu_busy_pct"] == 60.
    assert sample["host_iowait_pct"] == 20.
    assert sample["voluntary_context_switches"] == 3
    assert sample["involuntary_context_switches"] == 4
    write_cpu_snapshot(tmp_path, user=170, idle=130, iowait=30, ticks=1, identity="101")
    assert sampler.sample()["thread_cpu_pct"] == {}


def test_missing_or_malformed_proc_is_explicitly_unavailable(tmp_path):
    sampler = CpuSampler(proc=tmp_path)
    sample = sampler.sample()
    assert sample["host_cpu_busy_pct"] is None
    assert "host_cpu" in sample["unavailable"]
    write_cpu_snapshot(tmp_path, user=0, idle=0, iowait=0, ticks=0)
    (tmp_path / "self/task/12/stat").write_text("invalid")
    assert "thread:12" in sampler.sample()["unavailable"]


def test_regressing_clock_resets_cpu_delta_baseline(tmp_path):
    ticks = iter([2., 1., 3.])
    cpu = iter([1., 2., 3.])
    sampler = CpuSampler(proc=tmp_path, clock=lambda: next(ticks), process_time=lambda: next(cpu))
    assert sampler.sample()["process_cpu_pct"] is None
    assert sampler.sample()["process_cpu_pct"] is None
    assert sampler.sample()["process_cpu_pct"] == 50.


def test_inactive_measurement_window_serializes_as_strict_json():
    import json
    recorder = StageRecorder()
    recorder.window = (float("inf"), float("inf"))
    report = recorder.report()
    assert report["window"] is None
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("change", ["counter_regression", "topology_change"])
def test_host_cpu_resets_on_individual_counter_regression_or_topology_change(tmp_path, change):
    sampler = CpuSampler(proc=tmp_path)
    write_cpu_snapshot(tmp_path, user=100, idle=100, iowait=10, ticks=0)
    sampler.sample()
    if change == "counter_regression":
        write_cpu_snapshot(tmp_path, user=99, idle=120, iowait=11, ticks=0)
    else:
        write_cpu_snapshot(tmp_path, user=120, idle=110, iowait=10, ticks=0)
        with (tmp_path / "stat").open("a") as stream:
            stream.write("cpu0 120 0 0 110 10 0 0 0\n")
    assert sampler.sample()["host_cpu_busy_pct"] is None


def test_cpu_clock_failure_is_explicit_and_does_not_escape():
    def fail():
        raise OSError("clock unavailable")
    sample = CpuSampler(clock=fail).sample()
    assert sample["process_cpu_pct"] is None
    assert sample["unavailable"] == ["cpu_sampler_failed"]


def test_sampling_skips_clock_work_and_preserves_sparse_sink_measurements():
    ticks = iter([1., 2., 3., 4., 5., 6., 7., 8.])
    recorder = StageRecorder(sample_every=3, clock=lambda: next(ticks))
    for _ in range(5):
        with recorder.measure("tracking", "a"):
            pass
    for _ in range(2):
        with recorder.measure("sink_write_host_wall", "a"):
            pass
    series = {s["stage"]: s for s in recorder.report()["series"]}
    assert series["tracking"]["count"] == 2
    assert series["tracking"]["calls_all_phases"] == 5
    assert series["tracking"]["sample_every"] == 3
    assert series["sink_write_host_wall"]["count"] == 2
    assert series["sink_write_host_wall"]["sample_every"] == 1
    assert series["tracking"]["total_s"] == 2.


def test_external_observations_use_same_bounded_sampling_policy():
    recorder = StageRecorder(sample_every=2, max_series=1)
    for _ in range(5):
        recorder.observe("queue", 1., 2., "a")
    recorder.observe("other", 1., 2.)
    report = recorder.report()
    assert report["series"][0]["count"] == 3
    assert report["series"][0]["calls_all_phases"] == 5
    assert report["dropped_series"] == 1
    with pytest.raises(ValueError):
        StageRecorder(sample_every=0)


def test_cpu_sampling_interval_skips_proc_and_reports_actual_delta_boundaries(tmp_path):
    clocks = iter([1., 2., 3.])
    process = iter([1., 3.])
    sampler = CpuSampler(proc=tmp_path, clock=lambda: next(clocks),
                         process_time=lambda: next(process), minimum_interval_s=2.)
    assert sampler.sample()["process_cpu_pct"] is None
    assert sampler.sample()["unavailable"] == ["cpu_sample_not_scheduled"]
    sample = sampler.sample()
    assert sample["process_cpu_pct"] == 100.
    assert sample["interval_started_at"] == 1.
    assert sample["interval_finished_at"] == 3.
    for interval in (-1., float("inf")):
        with pytest.raises(ValueError):
            CpuSampler(minimum_interval_s=interval)
