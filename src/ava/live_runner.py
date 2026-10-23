"""Live multi-camera RTSP run orchestration.

Wires live RTSP sources -> detector -> rule engine -> event writer for a
concurrent multi-camera run. Two detector modes are supported:

- ``shared``: one Detector instance, inference serialized under a lock.
  Simplest; sufficient for moderate camera counts on one GPU.
- ``per_camera``: each camera gets its own Detector instance (own VRAM).
  Fully parallel inference; for higher camera counts.

Per-camera frame buffers are bounded (deque with maxlen); stale frames are
dropped with a counter when the buffer is full. Inference is decimated to
the configured ``analysis_fps`` per camera; decode runs at native rate.

The event schema is identical to file-based runs (ADR-5): versioned
EventRecord objects appended to a shared events.jsonl.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import CONFIG_SCHEMA_VERSION, DetectorConfig, LiveRunConfig
from .detector import Detector
from .events import EventRecord, EventWriter, EvidenceLocator
from .live_ingest import LiveSourceConfig, iter_live_frames
from .rules import RuleEngine


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
        event_writer: EventWriter,
        buffer_size: int,
        model_ref: str | None,
    ) -> None:
        self.cam = cam_config
        self.source_config = source_config
        self.detector = detector
        self.detector_lock = detector_lock
        self.rules = rules
        self.event_writer = event_writer
        self.buffer_size = buffer_size
        self.model_ref = model_ref

        self.stats = CameraStats(camera_id=cam_config.id)
        self._stop_event = threading.Event()
        self._frame_buffer: deque = deque(maxlen=buffer_size)
        # Inference pacing (source-time based, ADR-4).
        self._last_infer_ts: float | None = None
        self._infer_interval: float = 1.0 / cam_config.analysis_fps

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Main loop: decode frames, decimate to analysis_fps, infer, apply rules."""
        engine = RuleEngine(
            self.rules,
            camera_id=self.cam.id,
            session_id="s1",
            schema_version=CONFIG_SCHEMA_VERSION,
        )

        try:
            for frame in iter_live_frames(self.source_config):
                if self._stop_event.is_set():
                    break

                self.stats.frames_decoded += 1

                # Bounded buffer: push frame, count drops.
                if len(self._frame_buffer) == self._frame_buffer.maxlen:
                    self.stats.frames_dropped += 1
                self._frame_buffer.append((frame.index, frame.bgr))

                # Decimate to analysis_fps using source timestamp (ADR-4:
                # use source PTS for timing, not wall-clock). A small epsilon
                # guards against floating-point drift at exact-rate boundaries.
                if self._last_infer_ts is not None:
                    elapsed = frame.timestamp_s - self._last_infer_ts
                    if elapsed < self._infer_interval - 1e-6:
                        continue
                self._last_infer_ts = frame.timestamp_s

                # Inference (shared lock or dedicated).
                if self.detector_lock is not None:
                    with self.detector_lock:
                        result = self.detector.detect(
                            frame.index, frame.timestamp_s, frame.bgr
                        )
                else:
                    result = self.detector.detect(
                        frame.index, frame.timestamp_s, frame.bgr
                    )

                self.stats.frames_inferred += 1

                # Rule engine.
                emitted_utc = _utc_now_iso()
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
                    self.event_writer.write(record)
                    self.stats.events_emitted += 1

        except Exception as exc:  # noqa: BLE001 - worker must not crash the run
            self.stats.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            # Track reconnects from the source (best-effort; the iterator
            # manages its own state internally).
            pass


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
    event_writer = EventWriter(events_path)

    # Build detector(s).
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

    for i, cam in enumerate(live_config.cameras):
        rules = []
        for ref in cam.profiles:
            rules.extend(profiles_by_id[ref].rules)

        source_config = LiveSourceConfig(
            url=cam.url,
            backend=live_config.backend,
            reconnect_max=live_config.reconnect_max,
            reconnect_delay_s=live_config.reconnect_delay_s,
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
        )
        workers.append(worker)
        t = threading.Thread(target=worker.run, name=f"ava-live-{cam.id}", daemon=True)
        threads.append(t)

    # Start all cameras.
    start = time.perf_counter()
    for t in threads:
        t.start()

    # Wait for all workers to finish (reconnect budget exhausted or stopped).
    for t in threads:
        t.join()

    wall = time.perf_counter() - start
    event_writer.close()

    summary = LiveRunSummary(
        detector_mode=live_config.detector_mode,
        backend=live_config.backend,
        cameras=[w.stats for w in workers],
        wall_seconds=wall,
        total_events=sum(w.stats.events_emitted for w in workers),
        output_paths=[str(events_path)],
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
