# AVA technology stack

Short layered view of the project: what each technology is, what it does in AVA,
and why it was chosen. Exact version pins live in `requirements.lock.txt` and
the Compose file digests; this document names technologies and roles, not
versions. Reviewed at every stack change.

## Media ingestion

- **MediaMTX** (digest-pinned image) — RTSP origin between simulated streams
  and the analytics pipeline; standard media server doing on-the-fly protocol
  and streaming conversion with multi-subscriber fan-out. Why: one stable RTSP
  source layer lets any number of consumers replay identical footage without
  touching the publishers.
- **FFmpeg** (digest-pinned image) — normalizes raw source footage to uniform
  H.264/720p/10 FPS and loop-publishes it into MediaMTX. Why: deterministic
  input is a precondition for comparable benchmark points.

## Decode pipeline

- **GStreamer** — decode substrate for both paths. App path: OpenCV
  `VideoCapture` over an explicit `uridecodebin ! video/x-raw,BGR ! appsink
  drop=true max-buffers=N` pipeline; benchmark path: DeepStream NVDEC GPU
  decode. Why: one bounded-buffer contract (explicit queue, drop policy) across
  both paths keeps overload behavior and capacity claims comparable.

## Inference

Two deliberately separate stacks, each measured for its own purpose:

- **App path** — PyTorch (cu130 wheels) + Ultralytics (YOLO26n conventional;
  YOLOE-26s-seg prompted, ADR-3) + pinned CLIP commit for the text-prompt
  tokenizer, on the CUDA 12.6 runtime image. Why: the exact tested E-1 stack,
  reproducible from `requirements.lock.txt` in a clean build.
- **Benchmark path** — DeepStream + TensorRT with custom inference parsers
  (`benchmarks/e-3/nvdsinfer_custom_*.cpp`). Why: the vendor-optimized GPU
  pipeline where the capacity sweep and decode/resize ladders (4–50 streams)
  ran; it proved GPU headroom before any runtime commitment.

## Measurement

What makes AVA's claims checkable:

- **In-process Prometheus scrape surface** — live runs expose per-camera decode
  counts, inference percentiles, buffer drops and event latency. Why: capacity
  claims are instrumented evidence, not screenshots.
- **Source-PTS pacing gates** — request-rate quantization against the source
  time base is measured explicitly (the sub-4 FPS finding). Why: pacing
  artifacts are a first-class measurement target, not noise.
- **NVML** — GPU state and allocation checks before and during runs. Why:
  shared-GPU discipline; no run starts without knowing what else is on the
  device.

## Observability

- **Prometheus + Grafana + Loki + Alloy** (ops/ project, digest-pinned) —
  AVA-owned containerized study platform per ADR-15. Why: observability is
  part of the system under study, not an external dependency.

## Application layer

- **Python 3.12** (stdlib-first) — main language; standard for the CV/LLM
  ecosystem, keeps the runtime image small.
- **Pydantic v2 + PyYAML** — validated configuration contracts before any
  runtime work. Why: bad configs fail at load time with a message, not mid-run
  with wrong numbers.
- **pytest + pytest-cov + ruff** — test and lint gates under the ADR-13
  coverage floor.

## Evaluated, not active

PostgreSQL 18 (persistence, B-023), Kafka (event stream behind the PostgreSQL
outbox until its activation gates pass) and SeaweedFS (S3 evidence adapter for
F-3-S2) are selected in ADR-15/ADR-16 but not part of the running stack yet.
