"""Run orchestration.

Wires ingest -> detector -> output writers for a single configured run. Kept
deliberately small: E-1 runs one clip through one detector path sequentially.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .config import RunConfig
from .detector import ConventionalDetector, Detector, PromptedDetector
from .ingest import iter_frames, probe
from .output import AnnotatedVideoWriter, JsonlWriter
from .timing import PhaseTimer, peak_gpu_memory_bytes, reset_peak_gpu_memory


# Frames treated as warm-up (CUDA context, cuDNN autotune, tracker state init)
# and excluded from steady-state latency statistics.
WARMUP_FRAMES = 50


@dataclass
class RunSummary:
    """Measured facts about a completed run (feeds E-1-T5 baseline)."""

    source: str
    detector_kind: str
    frames: int = 0
    detections: int = 0
    wall_seconds: float = 0.0
    load_seconds: float = 0.0
    output_paths: list[str] = field(default_factory=list)
    # E-1-T5 phase breakdown (seconds), CUDA-synchronized on GPU runs.
    decode_seconds: float = 0.0
    inference_seconds: float = 0.0
    output_seconds: float = 0.0
    # Steady-state (post-warm-up) per-frame inference latency percentiles, s.
    infer_p50_s: float = 0.0
    infer_p95_s: float = 0.0
    infer_mean_s: float = 0.0
    # Peak CUDA memory allocated by the process during the run (bytes).
    peak_gpu_mem_bytes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


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
        summary.infer_p50_s = ordered[int(round(0.50 * (n - 1)))]
        summary.infer_p95_s = ordered[int(round(0.95 * (n - 1)))]
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
