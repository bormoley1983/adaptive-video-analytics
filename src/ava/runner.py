"""Run orchestration.

Wires ingest -> detector -> output writers for a single configured run. A
simple run processes one clip through one detector path sequentially; camera
runs apply rule profiles per camera with versioned event/evidence output
(ADR-4/ADR-5).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .config import RunConfig
from .detector import ConventionalDetector, Detector, PromptedDetector
from .events import EventRecord, EventWriter, EvidenceLocator, EvidenceStore
from .ingest import iter_frames, probe
from .output import AnnotatedVideoWriter, JsonlWriter
from .rules import RuleEngine, RuleEvent
from .timing import PhaseTimer, peak_gpu_memory_bytes, reset_peak_gpu_memory

# Frames treated as warm-up (CUDA context, cuDNN autotune, tracker state init)
# and excluded from steady-state latency statistics.
WARMUP_FRAMES = 50


@dataclass
class RunSummary:
    """Measured facts about a completed run (feeds the baseline report)."""

    source: str
    detector_kind: str
    frames: int = 0
    detections: int = 0
    wall_seconds: float = 0.0
    load_seconds: float = 0.0
    output_paths: list[str] = field(default_factory=list)
    # Phase breakdown (seconds), CUDA-synchronized on GPU runs.
    decode_seconds: float = 0.0
    inference_seconds: float = 0.0
    output_seconds: float = 0.0
    # Steady-state (post-warm-up) per-frame inference latency percentiles, s.
    infer_p50_s: float = 0.0
    infer_p95_s: float = 0.0
    infer_mean_s: float = 0.0
    # Peak CUDA memory allocated by the process during the run (bytes).
    peak_gpu_mem_bytes: int = 0
    # Rule-triggered events emitted during the run.
    events: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _utc_now_iso() -> str:
    """Wall-clock UTC in ISO-8601 (emitted_utc for live/replay runs)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _evidence_for(rule, event: RuleEvent, store: EvidenceStore,
                  frame_buffer: deque, fps: float):
    """Materialize evidence for one event per the rule's evidence options.

    Snapshots are written immediately from the ring buffer. Clips are
    deferred: the caller must call :func:`_flush_pending_clips` once enough
    post-event frames have accumulated (or at end of stream) so the clip
    window is centered on the triggering frame (ADR-5).
    """

    opts = rule.evidence
    if not (opts.snapshot or opts.clip):
        return None
    if opts.snapshot:
        # Snapshot of the triggering frame (kept in the ring buffer).
        for idx, bgr in frame_buffer:
            if idx == event.frame_index:
                return store.save_snapshot(event.camera_id, event.event_id, bgr)
        return None
    # Clip is deferred; return a sentinel so the caller knows to buffer it.
    return "DEFERRED_CLIP"


def _flush_pending_clips(
    pending: list[tuple[RuleEvent, object]],
    store: EvidenceStore,
    frame_buffer: deque,
    fps: float,
) -> list[EvidenceLocator]:
    """Materialize deferred clips whose post-event window is now available.

    A clip is flushed once the buffer contains frames up to at least
    ``event.frame_index + clip_seconds/2 * fps`` (or the stream has ended).
    The resulting window is centered on the triggering frame. Returns a list
    of evidence locators for the flushed clips (in order).
    """
    still_pending: list[tuple[RuleEvent, object]] = []
    flushed: list[EvidenceLocator] = []
    for event, rule in pending:
        opts = rule.evidence
        half_frames = round(fps * opts.clip_seconds / 2) if fps > 0 else 0
        target_index = event.frame_index + half_frames
        # Check if the buffer extends far enough past the triggering frame.
        buffer_max_idx = frame_buffer[-1][0] if frame_buffer else -1
        if buffer_max_idx >= target_index or len(frame_buffer) < (frame_buffer.maxlen or 0):
            # Build a centered window: half_frames before and after the event.
            frames_list = list(frame_buffer)
            # Find the triggering frame's position in the buffer.
            try:
                center_pos = next(
                    i for i, (idx, _) in enumerate(frames_list)
                    if idx == event.frame_index
                )
            except StopIteration:
                # Triggering frame evicted from buffer; use what we have.
                window = frames_list[-round(fps * opts.clip_seconds):]
            else:
                start = max(0, center_pos - half_frames)
                end = min(len(frames_list), center_pos + half_frames + 1)
                window = frames_list[start:end]
            locator = store.save_clip(event.camera_id, event.event_id, window, fps,
                                      opts.clip_seconds)
            flushed.append(locator)
        else:
            still_pending.append((event, rule))
    pending.clear()
    pending.extend(still_pending)
    return flushed


def build_detector(config: RunConfig) -> Detector:
    """Instantiate the configured detector path."""
    common = {
        "model_path": config.detector.model,
        "device": config.gpu,
        "confidence": config.detector.confidence,
        "iou": config.detector.iou,
    }
    if config.detector.kind == "prompted":
        return PromptedDetector(
            prompts=config.detector.prompts,
            text_encoder=config.detector.text_encoder,
            **common,
        )
    return ConventionalDetector(**common)


def run(config: RunConfig) -> RunSummary:
    """Execute one configured detection run end to end."""
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # Camera runs: each camera is replayed through the shared detector;
    # rules produce versioned events + evidence (ADR-5).
    if config.cameras:
        return _run_cameras(config)

    meta = probe(config.source)

    detector = build_detector(config)
    load_start = time.perf_counter()
    detector.load()
    load_seconds = time.perf_counter() - load_start

    summary = RunSummary(
        source=str(config.source),
        detector_kind=config.detector.kind,
        load_seconds=load_seconds,
    )

    jsonl_writer: JsonlWriter | None = None
    video_writer: AnnotatedVideoWriter | None = None
    if config.jsonl:
        jsonl_writer = JsonlWriter(config.output_dir / "detections.jsonl")
    if config.annotate and meta["fps"] > 0:
        video_writer = AnnotatedVideoWriter(
            config.output_dir / "annotated.mp4",
            meta["width"], meta["height"], meta["fps"],
        )

    # GPU runs synchronize the device at each phase boundary so inference cost
    # is attributed to inference, not leaked into the next decode. CPU smoke
    # runs skip the sync (no CUDA context) and stay fast.
    on_gpu = detector.device == "cuda"
    timer = PhaseTimer(sync=on_gpu)
    if on_gpu:
        reset_peak_gpu_memory()

    start = time.perf_counter()
    infer_steady: list[float] = []
    for frame in iter_frames(config.source):
        timer.begin("decode")
        # The generator already decoded `frame`; the decode cost is attributed
        # here as the interval since the previous phase ended. For an honest
        # split we time the inference and output phases explicitly and derive
        # decode as the residual of the per-frame wall time.
        timer.end("decode")

        timer.begin("inference")
        result = detector.detect(frame.index, frame.timestamp_s, frame.bgr)
        infer_elapsed = timer.end("inference")

        summary.frames += 1
        summary.detections += len(result.detections)
        if summary.frames > WARMUP_FRAMES:
            infer_steady.append(infer_elapsed)

        timer.begin("output")
        if jsonl_writer:
            jsonl_writer.write(result)
        if video_writer:
            video_writer.write(result, frame.bgr)
        timer.end("output")
    summary.wall_seconds = time.perf_counter() - start

    # Decode is the residual: total wall minus the explicitly timed inference
    # and output phases. This avoids double-counting the generator's decode work
    # while still reporting an honest three-way split.
    infer_total = timer.total("inference")
    output_total = timer.total("output")
    summary.inference_seconds = infer_total
    summary.output_seconds = output_total
    summary.decode_seconds = max(0.0, summary.wall_seconds - infer_total - output_total)

    if infer_steady:
        ordered = sorted(infer_steady)
        n = len(ordered)
        summary.infer_p50_s = ordered[round(0.50 * (n - 1))]
        summary.infer_p95_s = ordered[round(0.95 * (n - 1))]
        summary.infer_mean_s = sum(infer_steady) / n

    if on_gpu:
        summary.peak_gpu_mem_bytes = peak_gpu_memory_bytes()

    if jsonl_writer:
        jsonl_writer.close()
        summary.output_paths.append(str(jsonl_writer.path))
    if video_writer:
        video_writer.close()
        summary.output_paths.append(str(video_writer.path))
    return summary


def _run_cameras(config: RunConfig) -> RunSummary:
    """Camera run: replay each camera clip through the detector and rule engine.

    Produces per-camera detection JSONL, a shared versioned events.jsonl
    (ADR-5), and evidence files for rules that request them.
    """
    assert config.detector is not None  # validated by RunConfig

    detector = build_detector(config)
    load_start = time.perf_counter()
    detector.load()
    load_seconds = time.perf_counter() - load_start

    summary = RunSummary(
        source=",".join(str(c.source) for c in config.cameras),
        detector_kind=config.detector.kind,
        load_seconds=load_seconds,
    )

    profiles_by_id = {p.id: p for p in config.profiles}
    model_ref = str(config.detector.model)
    store = EvidenceStore(config.output_dir)

    # Run isolation: a fresh run must not append to a previous run's events.
    # If an events.jsonl already exists, start a new numbered file so reruns
    # are isolated (ADR-5).
    events_path = config.output_dir / "events.jsonl"
    if events_path.exists():
        counter = 1
        while (config.output_dir / f"events-{counter}.jsonl").exists():
            counter += 1
        events_path = config.output_dir / f"events-{counter}.jsonl"
    event_writer = EventWriter(events_path)

    on_gpu = detector.device == "cuda"
    timer = PhaseTimer(sync=on_gpu)
    if on_gpu:
        reset_peak_gpu_memory()

    start = time.perf_counter()
    for cam in config.cameras:
        meta = probe(cam.source)
        fps = meta["fps"] or 0.0

        # Build the rule set for this camera from its referenced profiles.
        rules = []
        for ref in cam.profiles:
            rules.extend(profiles_by_id[ref].rules)
        engine = RuleEngine(rules, camera_id=cam.id, session_id="s1")

        jsonl_writer = JsonlWriter(config.output_dir / f"{cam.id}.jsonl") \
            if config.jsonl else None
        video_writer = AnnotatedVideoWriter(
            config.output_dir / f"{cam.id}-annotated.mp4",
            meta["width"], meta["height"], fps,
        ) if (config.annotate and fps > 0) else None

        # Ring buffer of recent frames for evidence (snapshot/clip).
        # Size it to hold a full centered clip window (2 * clip_seconds).
        max_clip_s = max(
            (r.evidence.clip_seconds for r in rules if r.evidence.clip),
            default=3.0,
        )
        buf_len = max(1, round(fps * 2 * max_clip_s)) if fps > 0 else 60
        frame_buffer: deque = deque(maxlen=buf_len)

        # Deferred clips: (event, rule) pairs awaiting post-event frames.
        pending_clips: list[tuple[RuleEvent, object]] = []
        # Event records waiting for their clip evidence to be materialized.
        pending_records: list[tuple[RuleEvent, object]] = []

        for frame in iter_frames(cam.source):
            timer.begin("decode")
            timer.end("decode")

            timer.begin("inference")
            result = detector.detect(frame.index, frame.timestamp_s, frame.bgr)
            timer.end("inference")

            summary.frames += 1
            summary.detections += len(result.detections)

            # Keep the current frame in the buffer before rule evaluation so a
            # snapshot of the triggering frame can be materialized (the event's
            # frame_index is this frame).
            if any(r.evidence.snapshot or r.evidence.clip for r in rules):
                frame_buffer.append((frame.index, frame.bgr))

            # Rule evaluation (source-time based; discontinuities reset state).
            emitted_utc = _utc_now_iso()
            for event in engine.process_frame(result, emitted_utc):
                rule = next(r for r in rules if r.id == event.rule_id)
                evidence = _evidence_for(rule, event, store, frame_buffer, fps)
                if evidence == "DEFERRED_CLIP":
                    # Defer both the clip and the event record until the
                    # centered window is available.
                    pending_clips.append((event, rule))
                    pending_records.append((event, rule))
                else:
                    record = EventRecord.from_rule_event(
                        event,
                        config_version=config.schema_version,
                        model_ref=model_ref,
                        evidence=evidence,
                    )
                    event_writer.write(record)
                summary.events += 1

            # Flush any deferred clips whose post-event window is now ready.
            if pending_clips:
                flushed = _flush_pending_clips(
                    pending_clips, store, frame_buffer, fps
                )
                # Write the corresponding event records with evidence.
                for locator in flushed:
                    ev, _rl = pending_records.pop(0)
                    record = EventRecord.from_rule_event(
                        ev,
                        config_version=config.schema_version,
                        model_ref=model_ref,
                        evidence=locator,
                    )
                    event_writer.write(record)

            timer.begin("output")
            # The camera path always writes per-camera JSONL; only the
            # annotated video is optional.
            jsonl_writer.write(result)
            if video_writer:
                video_writer.write(result, frame.bgr)
            timer.end("output")

        # End of stream: flush any remaining deferred clips with whatever
        # frames are available (the window may be shorter than requested).
        if pending_clips:
            flushed = _flush_pending_clips(
                pending_clips, store, frame_buffer, fps
            )
            for locator in flushed:
                ev, _rl = pending_records.pop(0)
                record = EventRecord.from_rule_event(
                    ev,
                    config_version=config.schema_version,
                    model_ref=model_ref,
                    evidence=locator,
                )
                event_writer.write(record)

        jsonl_writer.close()
        summary.output_paths.append(str(jsonl_writer.path))
        if video_writer:
            video_writer.close()
            summary.output_paths.append(str(video_writer.path))

    event_writer.close()
    summary.output_paths.append(str(event_writer.path))
    summary.wall_seconds = time.perf_counter() - start

    infer_total = timer.total("inference")
    output_total = timer.total("output")
    summary.inference_seconds = infer_total
    summary.output_seconds = output_total
    summary.decode_seconds = max(0.0, summary.wall_seconds - infer_total - output_total)
    if on_gpu:
        summary.peak_gpu_mem_bytes = peak_gpu_memory_bytes()
    return summary
