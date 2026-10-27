# ADR-2: candidate DeepStream data path and independent GPU workers

Date: 2026-10-19 · Status: Proposed pending E-3 compatibility/quality gate · Scope: E-3/E-4/E-6.

## Context and decision

The reference host `ai-host1` has two RTX 3090s with separate 24 GB VRAM budgets; the ideas document also specifies 32 GB RAM and PCIe 4.0 x8 per GPU, still to verify in the runtime manifest. RTX 30-series is the project hardware floor; newer NVIDIA generations are in scope. The owner-supplied driver/Python/GPU allocation snapshot is in [ADR-1](ADR-1.md). Multi-camera performance depends on decoding, copies, batching, scheduling and source parameters as well as detector speed.

Recommend current stable DeepStream/GStreamer with NVDEC, TensorRT `nvinfer`,
and a validated tracker for the video hot path. DeepStream 9.2 stock-sample
compatibility is partially validated on the reference host; model export,
parser, parity and capacity evidence remain open. Use supported SDK examples
and Python Service Maker APIs where applicable; keep Python for control/domain
services and C++ for parsers/plugins that need it. Avoid deprecated legacy
Python bindings. Validate the SDK's own Python/runtime requirements
independently of the host's default interpreter.

MediaMTX/FFmpeg provide repeatable RTSP sources through a standalone CPU-only
Compose project. One MediaMTX container accepts one independent FFmpeg
publisher container per camera; publishers loop existing recordings mounted
read-only and expose stable camera paths. The replay stack owns no GPU, changes
no shared service and reports its resource use separately from the analytics
worker so source generation cannot inflate or hide capacity results.

Scale with independent workers pinned by GPU UUID. Assign each camera one
authoritative worker/assignment epoch; keep raw frames and model state local.
Central services exchange metadata and evidence references, not raw-frame
tensors. Start with the normally available shared AVA GPU. A two-GPU comparison
requires an explicitly approved window because the other device normally serves
local AI; it may be temporarily configured for tests but is not baseline AVA
capacity. Start with static assignments; add load awareness only after measured
imbalance. Bound per-camera buffers and discard stale analysis frames with
explicit counters rather than accumulating delay. Event publication has a
separate durability policy (ADR-5). The Python reference worker implements the
low-latency queue, pacing and measurement contracts in [ADR-17](ADR-17.md);
that reference behavior does not substitute for DeepStream capacity evidence.

## Alternatives and consequences

Python-only workers remain the fallback and reference implementation; they are simpler but may saturate decode/CPU earlier. Model parallelism across GPUs adds transfer/state complexity without pooling VRAM automatically. Kubernetes/Triton are deferred until multi-host operations or model serving requirements justify them. DeepStream adds NVIDIA coupling, export/parser work and tracker parity risks; support for a model in PyTorch does not certify support in this path.

## Validation and revisit

E-3-T2 first validates four independent replay streams, stable identities,
declared rates and reconnect after publisher/server restarts. E-3 then validates
a complete supported host/container matrix, one-stream sample, detector parity,
prompt export and measured stable limits before migration. E-4 measures scaling
and recovery. Revise if compatibility, quality, licensing or operational cost
fails, or if target hardware moves away from NVIDIA.

References checked 2026-10-19: NVIDIA [installation/compatibility matrix](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html), [Python Service Maker](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_service_maker_python.html), [DeepStream repository and build guidance](https://github.com/NVIDIA/deepstream), [MediaMTX introduction](https://mediamtx.org/docs/kickoff/introduction). Prefer the latest mutually compatible stable stack; document any older-version exception and preserve existing workloads.
