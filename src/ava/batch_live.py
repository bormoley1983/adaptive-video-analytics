"""Bounded camera decode, fair detection and independent result processing."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from dataclasses import asdict
from datetime import UTC, datetime
from queue import Empty, Queue

from .batch import BatchItem, BatchKey
from .batch_backend import ByteTrackCameraTracker, UltralyticsBatchDetector
from .batch_executor import BatchExecutor
from .batch_scheduler import BatchScheduler
from .config import CONFIG_SCHEMA_VERSION
from .events import EventRecord
from .live_ingest import LiveSourceConfig, iter_live_frames
from .live_metrics import _percentile, _query_gpu, _query_host
from .rules import RuleEngine
from .telemetry import Telemetry, new_run_context, serve_metrics


class _Camera:
    def __init__(self, config, rules, limit):
        self.config = config
        self.rules = rules
        self.limit = limit
        self.lock = threading.Lock()
        self.frames = deque()
        self.delivery = Queue(maxsize=1)
        self.ready = threading.Event()
        self.done = threading.Event()
        self.session = None
        self.decoded = 0
        self.terminal = Counter()
        self.measured = Counter()
        self.measured_decoded = 0
        self.completed_in_window = 0
        self.events = 0
        self.errors = []
        self.latencies = deque(maxlen=4096)
        self.latency_count = 0
        self.telemetry = None
        self.inference_duration_s = 0.
        self.window = None
        self.next_due = None
        self.cadence_session = None

    def in_window(self, when):
        return self.window is not None and self.window[0] <= when < self.window[1]

    def finish(self, item, reason):
        with self.lock:
            self.terminal[reason] += 1
            if self.telemetry is not None:
                if reason == "delivered":
                    self.telemetry.record_inference(self.config.id, self.inference_duration_s)
                elif reason != "cadence_skipped":
                    self.telemetry.record_drop(self.config.id, reason)
            if self.in_window(item.enqueued_at):
                self.measured[reason] += 1
            if reason == "delivered" and self.in_window(time.monotonic()):
                self.completed_in_window += 1

    def produce(self, live, stop, source):
        source_config = LiveSourceConfig(
            self.config.url, backend=live.backend, reconnect_max=live.reconnect_max,
            reconnect_delay_s=live.reconnect_delay_s, stop_event=stop)
        try:
            for frame in source(source_config):
                now = time.monotonic()
                item = BatchItem(BatchKey(self.config.id, frame.session_id or "s1", frame.index),
                                 frame, now)
                displaced = None
                with self.lock:
                    self.decoded += 1
                    if self.telemetry is not None:
                        self.telemetry.record_decode(self.config.id)
                    self.measured_decoded += int(self.in_window(now))
                    self.session = item.key.session_id
                    if len(self.frames) == self.limit:
                        displaced = self.frames.popleft()
                    self.frames.append(item)
                if displaced:
                    self.finish(displaced, "buffer_full")
                self.ready.set()
                if stop.is_set():
                    break
        except Exception as exc:  # noqa: BLE001 - preserve worker failure and cleanup evidence
            self.errors.append(str(exc) or type(exc).__name__)
            stop.set()
        finally:
            self.done.set()

    def take(self):
        with self.lock:
            return self.frames.popleft() if self.frames else None

    def eligible(self, item):
        timestamp = item.frame.timestamp_s
        if self.cadence_session != item.key.session_id:
            self.cadence_session = item.key.session_id
            self.next_due = timestamp
        if timestamp < self.next_due - 1e-6:
            return False
        interval = 1 / self.config.analysis_fps
        elapsed = int((timestamp - self.next_due + 1e-6) / interval)
        self.next_due += (elapsed + 1) * interval
        return True

    def process(self, stop, failure_stop, ack, sink, sink_lock, detector, live, tracker_factory):
        try:
            tracker = tracker_factory(self.config.analysis_fps)
        except Exception as exc:  # noqa: BLE001 - notify coordinator of worker startup failure
            self.errors.append(str(exc) or type(exc).__name__)
            failure_stop.set()
            return
        session = None
        engine = None
        while not stop.is_set() or not self.delivery.empty():
            try:
                delivery = self.delivery.get(timeout=.02)
            except Empty:
                continue
            item, result = delivery.item, delivery.result
            reason = "delivered"
            try:
                if item.key.session_id != self.session:
                    reason = "stale_session"
                elif result.failure:
                    reason = result.failure.code.value
                    if reason != "stale_session":
                        self.errors.append(result.failure.message)
                        failure_stop.set()
                else:
                    if session != item.key.session_id:
                        session = item.key.session_id
                        tracker.reset(session)
                        engine = RuleEngine(self.rules, self.config.id, session)
                    tracked = tracker.update(item, result)
                    emitted = datetime.now(UTC).isoformat()
                    for event in engine.process_frame(tracked, emitted):
                        record = EventRecord.from_rule_event(
                            event, config_version=CONFIG_SCHEMA_VERSION,
                            model_ref=str(detector.model), capture_utc=item.frame.capture_utc,
                            time_basis=item.frame.time_basis)
                        if live.slow_sink_s:
                            time.sleep(live.slow_sink_s)
                        with sink_lock:
                            inserted = sink.write(record)
                        if inserted:
                            self.events += 1
                            if self.telemetry is not None:
                                self.telemetry.record_event(self.config.id, time.monotonic() - item.enqueued_at)
                            if self.in_window(item.enqueued_at):
                                self.latencies.append(time.monotonic() - item.enqueued_at)
                                self.latency_count += 1
            except Exception as exc:  # noqa: BLE001 - preserve worker failure and cleanup evidence
                reason = "processing_error"
                self.errors.append(str(exc) or type(exc).__name__)
                failure_stop.set()
            finally:
                self.finish(item, reason)
                ack.put(item.key)

    def report(self, duration):
        with self.lock:
            samples = sorted(self.latencies)
            delivered = self.terminal["delivered"]
            return {
                "camera_id": self.config.id, "frames_decoded": self.decoded,
                "frames_inferred": delivered, "events_emitted": self.events,
                "errors": list(self.errors), "terminal": dict(self.terminal),
                "measured": {"decoded": self.measured_decoded,
                             "terminal": dict(self.measured),
                             "completed_in_window": self.completed_in_window,
                             "analyzed_fps": self.completed_in_window / duration if duration else None},
                "event_latency_s": {"count": self.latency_count,
                                    "samples": len(samples),
                                    "truncated": self.latency_count > len(samples),
                                    "p95": _percentile(samples, 95) if samples else None},
            }


def run_batched_live(live, detector, output_dir, gpu, sink, *, source=iter_live_frames,
                     backend_factory=None, tracker_factory=ByteTrackCameraTracker):
    """Run one common window after every source and the model become ready."""
    from .live_runner import CameraStats, LiveRunSummary

    profiles = {p.id: p.rules for p in live.profiles}
    cameras = {c.id: _Camera(c, [r for ref in c.profiles for r in profiles[ref]], live.buffer_size)
               for c in live.cameras}
    stop = threading.Event()
    processors_stop = threading.Event()
    ack = Queue(maxsize=len(cameras))
    sink_lock = threading.Lock()
    scheduler = BatchScheduler(cameras, live.batch.max_batch_size, live.batch.max_wait_ms,
                               live.batch.shutdown_timeout_s)
    executor = BatchExecutor(backend_factory or (lambda: UltralyticsBatchDetector(detector, gpu)))
    threads = []
    processors = []
    error = None
    window = None
    batches = deque(maxlen=65536)
    batch_count = 0
    queue_samples = deque(maxlen=4096)
    resource_samples = deque(maxlen=4096)
    sampler_stop = threading.Event()
    sampler = None
    sample_count = 0
    sampler_errors = []

    def sample_resources():
        nonlocal sample_count
        while not sampler_stop.is_set():
            sample = {"at": time.monotonic(), **_query_host()}
            gpu_sample = _query_gpu(gpu or "0") if gpu is not None else None
            sample["gpu"] = asdict(gpu_sample) if gpu_sample is not None else None
            resource_samples.append(sample)
            sample_count += 1
            try:
                with sink_lock:
                    if hasattr(sink, "check_connection"):
                        sink.check_connection()
            except Exception as exc:  # noqa: BLE001 - persistence health stops the whole run
                sampler_errors.append(str(exc) or type(exc).__name__)
                stop.set()
                return
            sampler_stop.wait(live.metrics_interval_s)
    started = time.monotonic()
    status = "failed"

    def drain_ack():
        while True:
            try:
                key = ack.get_nowait()
            except Empty:
                return
            scheduler.acknowledge(key)

    def receive():
        nonlocal batch_count, error, status
        result = executor.poll()
        if result is None:
            return
        batch_count += 1
        batches.append({"completed_at": time.monotonic(), "size": len(result.batch.items),
                        "execution_s": result.seconds})
        try:
            deliveries = scheduler.complete(result.batch, result.results)
        except ValueError as exc:
            error = str(exc)
            status = "failed"
            stop.set()
            deliveries = scheduler.fail_batch(result.batch, str(exc))
        for delivery in deliveries:
            camera = cameras[delivery.item.key.camera_id]
            camera.inference_duration_s = result.seconds
            camera.delivery.put_nowait(delivery)

    try:
        context = new_run_context(camera_ids=list(cameras), config_schema_version=CONFIG_SCHEMA_VERSION)
        telemetry = Telemetry(context, detector_mode="batched", backend=live.backend)
        if live.telemetry.prometheus_port is not None:
            serve_metrics(live.telemetry.prometheus_port, host=live.telemetry.prometheus_host)
        for camera in cameras.values():
            camera.telemetry = telemetry
        sampler = threading.Thread(target=sample_resources, daemon=True, name="batch-resource-sampler")
        sampler.start()
        executor.start()
        for camera in cameras.values():
            thread = threading.Thread(target=camera.produce, args=(live, stop, source), daemon=True,
                                      name=f"batch-decode-{camera.config.id}")
            threads.append(thread)
            thread.start()
            if not live.parallel_open and not camera.ready.wait(live.batch.readiness_timeout_s):
                raise RuntimeError(f"source readiness timeout: {camera.config.id}")
        deadline = started + live.batch.readiness_timeout_s
        while not (executor.ready.is_set() and all(c.ready.is_set() for c in cameras.values())):
            if stop.is_set() or time.monotonic() >= deadline or any(c.done.is_set() for c in cameras.values()):
                raise RuntimeError("source/model readiness failed")
            time.sleep(.005)
        if executor.error:
            raise executor.error
        for camera in cameras.values():
            thread = threading.Thread(target=camera.process,
                                      args=(processors_stop, stop, ack, sink, sink_lock, detector,
                                            live, tracker_factory), daemon=True,
                                      name=f"batch-process-{camera.config.id}")
            processors.append(thread)
            thread.start()
        window_start = time.monotonic() + live.batch.warmup_s
        window = (window_start, window_start + live.duration_s) if live.duration_s else None
        for camera in cameras.values():
            camera.window = window
        next_sample = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if window and now >= window[1]:
                break
            drain_ack()
            receive()
            for camera in cameras.values():
                current = camera.session
                if current and not scheduler.is_current(BatchKey(camera.config.id, current, 0)):
                    displaced = scheduler.start_session(camera.config.id, current)
                    if displaced:
                        camera.finish(displaced, "stale_session")
                item = camera.take()
                if item is None:
                    continue
                if item.key.session_id != camera.session:
                    camera.finish(item, "stale_session")
                    continue
                if not camera.eligible(item):
                    camera.finish(item, "cadence_skipped")
                    continue
                admission = scheduler.offer(item)
                if admission.displaced:
                    camera.finish(admission.displaced, "candidate_replaced")
                if not admission.accepted:
                    camera.finish(item, admission.reason)
            batch = scheduler.next_batch()
            if batch is not None:
                executor.submit(batch)
            if now >= next_sample:
                depths = {}
                ages = {}
                for camera_id, camera in cameras.items():
                    with camera.lock:
                        depths[camera_id] = len(camera.frames)
                        ages[camera_id] = now - camera.frames[0].enqueued_at if camera.frames else 0.
                    telemetry.sample_queue(camera_id, depths[camera_id], ages[camera_id])
                queue_samples.append({"at": now, "depths": depths, "oldest_age_s": ages,
                                      "scheduler": asdict(scheduler.snapshot())})
                next_sample = now + live.metrics_interval_s
            if any(c.done.is_set() and not stop.is_set() for c in cameras.values()):
                raise RuntimeError("source ended before measurement completed")
            if executor.error:
                raise executor.error
            time.sleep(.001)
        if stop.is_set():
            raise RuntimeError(error or "camera processing or source failure")
        status = "completed"
    except Exception as exc:  # noqa: BLE001 - preserve worker failure and cleanup evidence
        error = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
        sampler_stop.set()
        for delivery in scheduler.stop():
            cameras[delivery.item.key.camera_id].finish(delivery.item, "cancelled")
        deadline = time.monotonic() + live.batch.shutdown_timeout_s
        for thread in threads:
            thread.join(max(0., deadline - time.monotonic()))
        while time.monotonic() < deadline and scheduler.snapshot().in_flight:
            receive()
            drain_ack()
            time.sleep(.001)
        processors_stop.set()
        for thread in processors:
            thread.join(max(0., deadline - time.monotonic()))
        drain_ack()
        for delivery in scheduler.expire_shutdown():
            cameras[delivery.item.key.camera_id].finish(delivery.item, "shutdown_timeout")
        owner_done = executor.close(max(0., deadline - time.monotonic()))
        for camera in cameras.values():
            while (item := camera.take()) is not None:
                camera.finish(item, "cancelled")
        unresolved = [t.name for t in threads + processors if t.is_alive()]
        if not owner_done:
            unresolved.append("batch-model-owner")
        if error or unresolved or executor.cleanup_error or sampler_errors or any(c.errors for c in cameras.values()):
            status = "failed"
            error = error or "unresolved native work or model cleanup failure; discard process"
        if sampler is not None:
            sampler.join(max(0., deadline - time.monotonic()))
            if sampler.is_alive():
                unresolved.append(sampler.name)
                status = "failed"
                error = error or "resource sampler did not stop"
        reports = [c.report(live.duration_s if window else 0) for c in cameras.values()]
        report = {"status": status, "error": error, "measurement_window": window,
                  "cameras": reports, "unresolved_threads": unresolved,
                  "cleanup_error": str(executor.cleanup_error) if executor.cleanup_error else None,
                  "health_errors": sampler_errors,
                  "arithmetic": executor.backend.arithmetic if executor.backend is not None else {},
                  "batches": list(batches), "batch_count": batch_count,
                  "batch_samples_truncated": batch_count > len(batches),
                  "queue_samples": list(queue_samples),
                  "resource_samples": list(resource_samples),
                  "resource_samples_truncated": sample_count > len(resource_samples)}
        path = output_dir / "metrics.json"
        path.write_text(json.dumps(report, indent=2))
    if status != "completed":
        raise RuntimeError(error or "batched run failed")
    stats = [CameraStats(c.config.id, frames_decoded=c.decoded,
                         frames_inferred=c.terminal["delivered"],
                         frames_dropped=c.decoded - c.terminal["delivered"] - c.terminal["cadence_skipped"],
                         events_emitted=c.events, errors=list(c.errors)) for c in cameras.values()]
    return LiveRunSummary("batched", live.backend, stats, time.monotonic() - started,
                          sum(c.events for c in cameras.values()), [str(path)])
