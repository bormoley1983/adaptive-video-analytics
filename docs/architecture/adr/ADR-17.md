# ADR-17: bounded low-latency live frame flow and absolute analysis cadence

Date: 2026-10-25 · Status: Accepted; reference flow validated, capacity pending · Scope: E-3/E-4.

## Context and decision

The first E-3-T5 sweep exposed two independent defects in the Python live
reference path. Its relative source-PTS gate selected 10 FPS frames at 0.3-second
intervals for a 4 FPS request, realizing about 3.33 FPS. Its bounded deque was
never consumed, so the reported drops, queue depth and queue age described a
history ring rather than processing backpressure. Event latency recorded only
detector-call duration. Those measurements cannot certify capacity.

Use one decode producer and one analysis consumer per camera, joined by a
bounded processing queue. When the queue is full, discard the oldest queued
frame and count that displacement once; do not block live ingestion until stale
latency accumulates. Queue depth is the number of frames awaiting analysis and
oldest age is measured from monotonic enqueue time. Evidence history is a
separate concern and must not reuse the processing queue implicitly.

Schedule analysis against an absolute source-time deadline. After selecting a
frame, advance the deadline by the number of elapsed analysis intervals rather
than anchoring the next interval to that frame. This allows discrete source
frames to alternate around a non-integral target interval: a 10 FPS source can
realize an average 4 FPS cadence through 0.2/0.3-second gaps. The requested rate
never creates frames and therefore cannot exceed source delivery rate.

Measure event latency from monotonic AVA enqueue time through event persistence.
Treat source-to-event latency as unavailable unless an independently comparable
capture clock exists. Carry live session identity on frames; reconnect starts a
new cadence and rule engine. A per-camera detector resets tracker state at that
boundary. Conventional shared-detector mode remains unsuitable for correctness
claims because its persistent ByteTrack state is shared across cameras; resolve
that through per-camera tracker state or a batched detection/tracking split.

## Alternatives and consequences

Increasing the old deque would retain more BGR frames without affecting
inference and would increase RAM use. Removing all buffering would couple decode
availability to detector latency and conceal overload inside capture backends.
Back-pressuring the RTSP source preserves frames but is incompatible with the
low-latency live policy when inference falls behind. The bounded drop-oldest
queue makes overload explicit and keeps memory bounded, at the cost of declared
frame loss under load.

The Python path remains a reference and diagnostic implementation. Four-camera
GPU diagnostics sustained both 4 FPS and the approximately 10 FPS source rate
without backlog or drops. A later 50-camera, 10 FPS target reached only 4.20
analyzed FPS/camera on average: every queue reached 40 frames and 51,265 frames
were displaced. GPU utilization averaged 33.77% and peaked at 50%, so this is an
execution-path bottleneck rather than evidence that GPU compute is exhausted.
The test does not isolate Python threads as the sole cause; independent model
instances, concurrent CUDA submission, software decode and host memory pressure
remain contributors to measure.

This decision does not replace ADR-2's DeepStream/NVDEC hot-path candidate and
does not itself establish GPU capacity. Central single-model micro-batching and
per-camera tracker isolation are decided separately in [ADR-18](ADR-18.md).

## Validation and revisit

Deterministic tests must cover integral and non-integral source/analysis rates,
targets above source rate, queue overflow, timestamp-derived queue age,
ingest-to-persistence event latency, stop behavior and reconnect/session reset.
Short GPU-0 diagnostics may run for two to five minutes because the earlier
35-minute sweep already supplies stability evidence; they do not retroactively
validate its broken queue and latency metrics.

Revisit queue policy for evidence capture, variable-frame-rate sources,
hardware decode or variable latency budgets. ADR-18 now owns central
micro-batching and per-camera tracker isolation. Any stable-capacity claim still
follows ADR-6 and requires a declared drop/quality tolerance plus a valid
long-duration result.
