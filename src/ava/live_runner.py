"""Live multi-camera RTSP run orchestration.

Wires live RTSP sources -> detector -> rule engine -> event writer for a
concurrent multi-camera run. Two detector modes are supported:

- ``shared``: one Detector instance, inference serialized under a lock.
  Simplest; sufficient for moderate camera counts on one GPU.
- ``per_camera``: each camera gets its own Detector instance (own VRAM).
  Fully parallel inference; for higher camera counts.

Each camera has an independent decode producer and inference consumer joined
by a bounded queue. When inference falls behind, the oldest queued frame is
dropped to preserve live latency. Inference follows an absolute source-time
cadence at the configured ``analysis_fps`` per camera.

The event schema is identical to file-based runs (ADR-5): versioned
EventRecord objects appended to a shared events.jsonl.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import floor
from pathlib import Path

from .config import CONFIG_SCHEMA_VERSION, DetectorConfig, LiveRunConfig
from .detector import Detector
from .events import EventRecord, EvidenceLocator
from .ingest import Frame
from .live_ingest import LiveSourceConfig, iter_live_frames
from .live_metrics import CameraMetrics, LiveMetricsCollector
from .persistence.ports import EventSink
from .rules import RuleEngine
from .telemetry import Telemetry, new_run_context, serve_metrics


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class CameraStats:
    """Per-camera runtime statistics for a live run."""

    camera_id: str
    frames_decoded: int = 0
    frames_inferred: int = 0
    frames_dropped: int = 0
    events_emitted: int = 0
    reconnects: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "frames_decoded": self.frames_decoded,
            "frames_inferred": self.frames_inferred,
            "frames_dropped": self.frames_dropped,
            "events_emitted": self.events_emitted,
            "reconnects": self.reconnects,
            "errors": list(self.errors),
        }


@dataclass
class LiveRunSummary:
    """Measured facts about a completed (or stopped) live run."""

    detector_mode: str
    backend: str
    cameras: list[CameraStats] = field(default_factory=list)
    wall_seconds: float = 0.0
    total_events: int = 0
    output_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "detector_mode": self.detector_mode,
            "backend": self.backend,
            "cameras": [c.to_dict() for c in self.cameras],
            "wall_seconds": self.wall_seconds,
            "total_events": self.total_events,
            "output_paths": list(self.output_paths),
        }


@dataclass(frozen=True)
class _QueuedFrame:
    frame: Frame
    enqueued_at: float


class _AnalysisCadence:
    """Select frames against an absolute source-time schedule."""

    def __init__(self, analysis_fps: float) -> None:
        self.interval_s = 1.0 / analysis_fps
        self._next_due_s: float | None = None
        self._session_id: str | None = None

    def should_analyze(self, timestamp_s: float, session_id: str) -> bool:
        if self._session_id != session_id or self._next_due_s is None:
            self._session_id = session_id
            self._next_due_s = timestamp_s

        if timestamp_s < self._next_due_s - 1e-6:
            return False

        intervals_elapsed = floor(
            (timestamp_s - self._next_due_s + 1e-6) / self.interval_s
        )
        self._next_due_s += (intervals_elapsed + 1) * self.interval_s
        return True


class _CameraWorker:
    """Processes one camera's frame stream: decode -> infer -> rules -> events.

    Runs in its own thread. The detector call is either protected by a shared
    lock (shared mode) or uses a dedicated instance (per_camera mode).
    """

    def __init__(
        self,
        cam_config,
        source_config: LiveSourceConfig,
        detector: Detector,
        detector_lock: threading.Lock | None,
        rules: list,
        event_writer: EventSink,
        buffer_size: int,
        model_ref: str | None,
        metrics: CameraMetrics | None = None,
        slow_sink_s: float = 0.0,
        telemetry: Telemetry | None = None,
        on_sink_error=None,
    ) -> None:
        self.cam = cam_config
        self.source_config = source_config
        self.detector = detector
        self.detector_lock = detector_lock
        self.rules = rules
        self.event_writer = event_writer
        self.buffer_size = buffer_size
        self.model_ref = model_ref
        self.slow_sink_s = slow_sink_s
        self.telemetry = telemetry
        self.on_sink_error = on_sink_error

        self.stats = CameraStats(camera_id=cam_config.id)
        self.metrics = metrics or CameraMetrics(camera_id=cam_config.id)
        self._stop_event = threading.Event()
        # Shared with the ingest iterator so a stop signal interrupts an
        # in-flight cap.read() promptly (bounded runs, external stops).
        source_config.stop_event = self._stop_event
        self._frame_buffer: deque[_QueuedFrame] = deque(maxlen=buffer_size)
        self._queue_condition = threading.Condition()
        self._producer_done = False
        self._producer_error: Exception | None = None
        self._cadence = _AnalysisCadence(cam_config.analysis_fps)

    def stop(self) -> None:
        self._stop_event.set()
        with self._queue_condition:
            self._queue_condition.notify_all()

    def _enqueue_frame(self, frame) -> None:
        queued = _QueuedFrame(frame=frame, enqueued_at=time.perf_counter())
        with self._queue_condition:
            if len(self._frame_buffer) == self._frame_buffer.maxlen:
                self._frame_buffer.popleft()
                self.stats.frames_dropped += 1
                self.metrics.record_drop_buffer()
                if self.telemetry is not None:
                    self.telemetry.record_drop(self.cam.id, "buffer_full")
            self._frame_buffer.append(queued)
            self._queue_condition.notify()

    def _dequeue_frame(self) -> _QueuedFrame | None:
        with self._queue_condition:
            while not self._frame_buffer and not self._producer_done:
                if self._stop_event.is_set():
                    return None
                self._queue_condition.wait(timeout=0.1)
            if self._stop_event.is_set() or not self._frame_buffer:
                return None
            return self._frame_buffer.popleft()

    def queue_snapshot(self) -> tuple[int, float]:
        """Return current queue depth and oldest-frame age."""
        with self._queue_condition:
            depth = len(self._frame_buffer)
            if not depth:
                return 0, 0.0
            age_s = max(0.0, time.perf_counter() - self._frame_buffer[0].enqueued_at)
            return depth, age_s

    def _produce_frames(self) -> None:
        try:
            for frame in iter_live_frames(self.source_config):
                if self._stop_event.is_set():
                    break
                self.stats.frames_decoded += 1
                self.metrics.record_decode()
                if self.telemetry is not None:
                    self.telemetry.record_decode(self.cam.id)
                self._enqueue_frame(frame)
        except Exception as exc:  # noqa: BLE001 - surfaced on the worker
            self._producer_error = exc
        finally:
            with self._queue_condition:
                self._producer_done = True
                self._queue_condition.notify_all()

    def run(self) -> None:
        """Consume queued frames, pace inference, apply rules and write events."""
        current_session_id: str | None = None
        engine: RuleEngine | None = None
        producer = threading.Thread(
            target=self._produce_frames,
            name=f"ava-decode-{self.cam.id}",
            daemon=True,
        )
        producer.start()

        try:
            while True:
                queued = self._dequeue_frame()
                if queued is None:
                    break
                frame = queued.frame

                session_id = frame.session_id or "s1"
                if session_id != current_session_id:
                    if current_session_id is not None and self.detector_lock is None:
                        self.detector.reset_tracker()
                    current_session_id = session_id
                    engine = RuleEngine(
                        self.rules,
                        camera_id=self.cam.id,
                        session_id=session_id,
                        schema_version=CONFIG_SCHEMA_VERSION,
                    )

                if not self._cadence.should_analyze(frame.timestamp_s, session_id):
                    continue

                # Inference (shared lock or dedicated) with timing.
                infer_start = time.perf_counter()
                if self.detector_lock is not None:
                    with self.detector_lock:
                        result = self.detector.detect(
                            frame.index, frame.timestamp_s, frame.bgr
                        )
                else:
                    result = self.detector.detect(
                        frame.index, frame.timestamp_s, frame.bgr
                    )
                infer_duration = time.perf_counter() - infer_start

                self.stats.frames_inferred += 1
                self.metrics.record_inference(infer_duration)
                if self.telemetry is not None:
                    self.telemetry.record_inference(self.cam.id, infer_duration)

                # Rule engine.
                emitted_utc = _utc_now_iso()
                assert engine is not None  # initialized for this queued frame
                events = engine.process_frame(result, emitted_utc)
                for event in events:
                    record = EventRecord.from_rule_event(
                        event,
                        config_version=CONFIG_SCHEMA_VERSION,
                        model_ref=self.model_ref,
                        evidence=EvidenceLocator(),
                        capture_utc=frame.capture_utc,
                        time_basis=frame.time_basis,
                    )
                    if self.slow_sink_s > 0:
                        time.sleep(self.slow_sink_s)
                    try:
                        inserted = self.event_writer.write(record)
                    except Exception as exc:
                        if self.on_sink_error is not None:
                            self.on_sink_error(exc)
                        raise
                    if inserted:
                        self.stats.events_emitted += 1
                        event_latency = time.perf_counter() - queued.enqueued_at
                        self.metrics.record_event(event_latency)
                        if self.telemetry is not None:
                            self.telemetry.record_event(self.cam.id, event_latency)

            if self._producer_error is not None:
                raise self._producer_error

        except Exception as exc:  # noqa: BLE001 - worker must not crash the run
            self.stats.errors.append(f"{type(exc).__name__}: {exc}")
            self.metrics.record_error(f"{type(exc).__name__}: {exc}")
            if self.telemetry is not None:
                self.telemetry.record_error(self.cam.id, "other")
        finally:
            self._stop_event.set()
            with self._queue_condition:
                self._queue_condition.notify_all()
            producer.join(timeout=2.0)


def run_live(
    live_config: LiveRunConfig,
    detector_config: DetectorConfig,
    output_dir: Path,
    gpu: str | None = "0",
) -> LiveRunSummary:
    """Execute a live multi-camera RTSP run.

    Blocks until all cameras are stopped (via :func:`stop_live_run`) or they
    exhaust their reconnect budgets. Returns a summary with per-camera stats.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Event writer (shared across cameras).
    events_path = output_dir / "events.jsonl"
    if events_path.exists():
        counter = 1
        while (output_dir / f"events-{counter}.jsonl").exists():
            counter += 1
        events_path = output_dir / f"events-{counter}.jsonl"

    from .persistence.compose import build_event_sink
    event_writer: EventSink = build_event_sink(
        live_config.persistence, jsonl_path=events_path
    )

    try:
      return _run_live_body(
          live_config, detector_config, output_dir, gpu, event_writer
      )
    finally:
      event_writer.close()


def _run_live_body(
    live_config: LiveRunConfig,
    detector_config: DetectorConfig,
    output_dir: Path,
    gpu: str | None,
    event_writer: EventSink,
) -> LiveRunSummary:
    """Inner body of run_live; the sink is closed by the caller's finally."""
    common = {
        "model_path": detector_config.model,
        "device": gpu,
        "confidence": detector_config.confidence,
        "iou": detector_config.iou,
    }

    if live_config.detector_mode == "per_camera":
        detectors: list[Detector] = []
        for _ in live_config.cameras:
            det = _build_detector(detector_config, **common)
            det.load()
            detectors.append(det)
        detector_lock = None
    else:
        shared_det = _build_detector(detector_config, **common)
        shared_det.load()
        detectors = [shared_det] * len(live_config.cameras)
        detector_lock = threading.Lock()

    # Build per-camera rule sets.
    profiles_by_id = {p.id: p for p in live_config.profiles}
    model_ref = str(detector_config.model)

    workers: list[_CameraWorker] = []
    threads: list[threading.Thread] = []
    sink_errors: list[Exception] = []
    sink_error_lock = threading.Lock()

    def stop_for_sink_error(exc: Exception) -> None:
        with sink_error_lock:
            if not sink_errors:
                sink_errors.append(exc)
        for worker in workers:
            worker.stop()

    for i, cam in enumerate(live_config.cameras):
        rules = []
        for ref in cam.profiles:
            rules.extend(profiles_by_id[ref].rules)

        def _on_reconnect(
            attempt: int, session_id: str, _cam_id: str = cam.id
        ) -> None:
            # Runs on the camera's worker thread; stats are per-worker.
            for w in workers:
                if w.cam.id == _cam_id:
                    w.stats.reconnects += 1
                    w.metrics.record_reconnect()
                    break
            if telemetry is not None:
                telemetry.record_reconnect(_cam_id)
            run_ctx.log(
                "live_run.reconnect",
                "warning",
                "camera stream reconnected",
                camera_id=_cam_id,
                attempt=attempt,
                session_id=session_id,
            )

        source_config = LiveSourceConfig(
            url=cam.url,
            backend=live_config.backend,
            reconnect_max=live_config.reconnect_max,
            reconnect_delay_s=live_config.reconnect_delay_s,
            on_reconnect=_on_reconnect,
        )

        worker = _CameraWorker(
            cam_config=cam,
            source_config=source_config,
            detector=detectors[i],
            detector_lock=detector_lock,
            rules=rules,
            event_writer=event_writer,
            buffer_size=live_config.buffer_size,
            model_ref=model_ref,
            slow_sink_s=live_config.slow_sink_s,
            on_sink_error=stop_for_sink_error,
        )
        workers.append(worker)
        t = threading.Thread(target=worker.run, name=f"ava-live-{cam.id}", daemon=True)
        threads.append(t)

    # Metrics collector.
    collector = LiveMetricsCollector(
        gpu_index=gpu or "0",
        sample_interval_s=live_config.metrics_interval_s,
    )
    for cam in live_config.cameras:
        collector.register_camera(cam.id)

    # Structured telemetry (B-021): correlation context + bounded Prometheus
    # metrics. The scrape endpoint is optional and loopback-only.
    run_ctx = new_run_context(
        camera_ids=[cam.id for cam in live_config.cameras],
        config_schema_version=CONFIG_SCHEMA_VERSION,
    )
    telemetry = Telemetry(
        context=run_ctx,
        detector_mode=live_config.detector_mode,
        backend=live_config.backend,
    )
    metrics_thread = None
    if live_config.telemetry.prometheus_port is not None:
        metrics_thread = serve_metrics(
            live_config.telemetry.prometheus_port,
            host=live_config.telemetry.prometheus_host,
        )

    # Wire per-camera metrics into workers.
    for i, worker in enumerate(workers):
        cam_id = live_config.cameras[i].id
        worker.metrics = collector.get_camera(cam_id)  # type: ignore[assignment]
        worker.telemetry = telemetry

    run_ctx.log(
        "live_run.start",
        "info",
        "live run starting",
        cameras=len(live_config.cameras),
        detector_mode=live_config.detector_mode,
        backend=live_config.backend,
        prometheus_port=live_config.telemetry.prometheus_port,
    )
    if detector_config.kind == "conventional" and live_config.detector_mode == "shared":
        run_ctx.log(
            "live_run.tracker_isolation",
            "warning",
            "shared conventional detector reuses persistent tracker state across cameras",
        )

    # Background host/GPU sampler thread; also samples bounded queue state so
    # depth/age appear in both metrics.json and the Prometheus gauges.
    sampler_stop = threading.Event()

    def _sampler_loop() -> None:
        while not sampler_stop.is_set():
            if hasattr(event_writer, "check_connection"):
                try:
                    event_writer.check_connection()
                except Exception as exc:  # noqa: BLE001 - stop on any sink failure
                    stop_for_sink_error(exc)
                    return
            collector.sample_host()
            for worker in workers:
                cam_id = worker.cam.id
                depth, age_s = worker.queue_snapshot()
                cm = collector.get_camera(cam_id)
                if cm is not None:
                    cm.sample_queue(depth, age_s)
                telemetry.sample_queue(cam_id, depth, age_s)
            sampler_stop.wait(collector.sample_interval_s)

    sampler_thread = threading.Thread(target=_sampler_loop, daemon=True)

    # Start all cameras.
    start = time.perf_counter()
    for t in threads:
        t.start()
    sampler_thread.start()
    telemetry.mark_run_started()

    # Bounded run: stop every camera after the configured wall-clock window so
    # infinite streams still terminate cleanly and write their summary.
    duration_timer = None
    if live_config.duration_s > 0:
        def _stop_all_workers() -> None:
            for worker in workers:
                worker.stop()

        duration_timer = threading.Timer(live_config.duration_s, _stop_all_workers)
        duration_timer.daemon = True
        duration_timer.start()
        run_ctx.log(
            "live_run.bounded",
            "info",
            "bounded run duration armed",
            duration_seconds=live_config.duration_s,
        )

    # Wait for all workers to finish (reconnect budget exhausted or stopped).
    for t in threads:
        t.join()
    if duration_timer is not None:
        duration_timer.cancel()

    sampler_stop.set()
    sampler_thread.join()

    wall = time.perf_counter() - start
    if sink_errors:
        raise RuntimeError("Event persistence failed; live run stopped") from sink_errors[0]

    # Write metrics.
    for cam in live_config.cameras:
        cm = collector.get_camera(cam.id)
        if cm is not None:
            cm.finish()
    metrics_path = collector.write(output_dir / "metrics.json")

    for worker in workers:
        run_ctx.log(
            "live_run.camera_done",
            "info" if not worker.stats.errors else "warning",
            "camera finished",
            camera_id=worker.cam.id,
            frames_decoded=worker.stats.frames_decoded,
            frames_inferred=worker.stats.frames_inferred,
            frames_dropped=worker.stats.frames_dropped,
            events_emitted=worker.stats.events_emitted,
            reconnects=worker.stats.reconnects,
            errors=len(worker.stats.errors),
        )
    run_ctx.log(
        "live_run.end",
        "info",
        "live run finished",
        wall_seconds=round(wall, 3),
        total_events=sum(w.stats.events_emitted for w in workers),
    )

    if metrics_thread is not None:
        # Daemon thread; nothing to join. The server socket closes with the
        # process. Kept explicit so a future non-daemon design stays correct.
        pass

    summary = LiveRunSummary(
        detector_mode=live_config.detector_mode,
        backend=live_config.backend,
        cameras=[w.stats for w in workers],
        wall_seconds=wall,
        total_events=sum(w.stats.events_emitted for w in workers),
        output_paths=([str(event_writer.path)] if hasattr(event_writer, "path") else [])
        + [str(metrics_path)],
    )
    return summary


def _build_detector(detector_config: DetectorConfig, **common) -> Detector:
    """Instantiate the configured detector path (mirrors runner.build_detector)."""
    if detector_config.kind == "prompted":
        from .detector import PromptedDetector

        return PromptedDetector(
            prompts=detector_config.prompts,
            text_encoder=detector_config.text_encoder,
            **common,
        )
    from .detector import ConventionalDetector

    return ConventionalDetector(**common)
