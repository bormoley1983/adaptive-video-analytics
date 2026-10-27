# ADR-18: deadline-bounded single-model micro-batching per GPU

Date: 2026-10-25 · Status: Accepted for staged implementation; capacity pending · Scope: E-3/E-4.

## Context

ADR-17 corrected the Python live path's cadence, queue and latency contracts.
Four conventional streams then sustained their approximately 10 FPS source
rate, but a 300-second 50-camera diagnostic did not. Analyzed throughput
averaged 4.20 FPS/camera, all 40-frame queues filled and 51,265 frames were
dropped. Inference p95 averaged 338.84 ms even though GPU utilization averaged
33.77%, peaked at 50%, and VRAM peaked at 5,397 MiB.

The current `per_camera` mode loads one model per camera and lets many Python
threads invoke CUDA independently. The `shared` mode serializes calls through
one lock and, for the conventional detector, incorrectly shares persistent
ByteTrack state across cameras. Neither mode presents a sufficiently large,
ordered unit of work to the GPU while preserving camera-local tracking.

The measurements prove an execution-path bottleneck, not that processor threads
alone are its cause. Software decode, frame copies, CUDA submission, model
instances, scheduling and host memory all require separate attribution.

## Decision

Add a `batched` live detector mode while retaining `shared` and `per_camera` as
compatibility/reference modes during migration. One GPU executor owns one loaded
model and CUDA context per worker GPU. It receives eligible frames from a fair
micro-batch scheduler and performs one detector call for the batch.

Each camera retains its ADR-17 bounded drop-oldest ingress queue and absolute
analysis cadence. The scheduler admits at most one frame per camera per batch,
allows at most one in-flight frame per camera, and chooses ready cameras fairly
by deadline/round-robin order. It flushes when either the configured maximum
batch size is reached or the oldest admitted item reaches the configured wait
limit. Initial defaults are 16 frames and 20 ms; both are explicit configuration
and benchmark dimensions rather than universal constants.

Batch results carry camera ID, session ID, frame index and source timestamp and
are demultiplexed without losing per-camera order. Detection and tracking become
separate interfaces: the shared model produces detections, while every camera
owns independent ByteTrack, rule and cadence state. A session change removes
stale queued/results work for the previous session and resets that camera's
tracker and rule state. Cross-camera tracker sharing is prohibited in batched
mode.

The GPU executor is a bounded single owner, not an unbounded task pool. A batch
failure is attributed to every included item and surfaced without corrupting
other camera sessions. Result delivery, event persistence and telemetry remain
bounded; slow downstream work must not occupy the GPU executor.

Load and warm the model before measurement. Start camera connections
concurrently, declare a readiness timeout, complete a warm-up interval, then
open one common measurement window for ready cameras. Report cameras that miss
readiness separately; do not give later cameras shorter measurement windows.

The first implementation remains Python/Ultralytics so the corrected reference
contracts can be preserved and measured. Hardware decode and the
DeepStream/TensorRT path remain separate follow-up work under ADR-2. A batched
Python improvement does not settle the production runtime choice.

## Alternatives and consequences

Keeping one model per camera is simple but duplicates state, creates competing
CUDA submission paths and already fails the 50-camera diagnostic. Serializing
single frames through one shared detector preserves one model but cannot exploit
batch parallelism and couples all cameras to lock wait. Allowing multiple frames
from one camera per batch can improve fill but harms fairness and complicates
ordered tracking, so it is excluded from the first slice.

A fixed full-batch wait maximizes batch size but adds avoidable latency during
light load. Immediate single-frame execution minimizes scheduler delay but
recreates the underfilled GPU workload. The size-or-deadline policy bounds both.

One model reduces VRAM and initialization time and should improve GPU occupancy.
It adds scheduling, correlation, failure and tracker-separation complexity.
Batching may change numerical/post-processing behavior, so correctness and
quality comparisons remain mandatory. CPU decode can still become the next
bottleneck even if inference is fixed.

## Validation and revisit

Deterministic tests cover size and deadline flush, sparse load, fairness, one
in-flight item per camera, per-camera ordering, stale-session rejection, queue
overflow, stop/drain behavior, partial/batch failure and independent tracker
reset. Integration tests use a fake batch detector to prove correlation through
rules and event output. GPU tests compare batch sizes 8, 16 and 32 and report
batch fill, wait, execution, demultiplex, queue and end-to-end latency.

The first five-minute 50-camera/10 FPS diagnostic target is, for every ready
camera, at least 9.5 analyzed FPS, at most 1% post-warm-up drops, no sustained
queue growth and p95 ingest-to-event latency below 3 seconds. This is an
optimization gate, not stable-capacity certification. ADR-6 still requires a
declared quality/drop tolerance and at least 30 minutes for a passing capacity
point.

Revisit the 16/20 ms defaults after measured sweeps; use workload-specific
values when documented. Escalate to NVDEC/DeepStream/TensorRT if decode, copies,
Python scheduling or model support prevents the target. Revisit multi-executor
GPU concurrency only after a single executor is profiled and fairness remains
preserved.
