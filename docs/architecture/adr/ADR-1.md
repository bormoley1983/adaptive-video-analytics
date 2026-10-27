# ADR-1: start with a modular Python CLI PoC

Date: 2026-10-19 · Revised: 2026-10-20 (measured E-1 execution) · Status: Accepted and validated for the PoC · Scope: E-1/E-2.

## Context and decision

The immediate need is an inspectable comparison on a single MP4, then configurable alerts. Choose current stable Python/PyTorch CUDA, Ultralytics, ByteTrack, OpenCV/FFmpeg, Pydantic, YAML/JSON configuration and JSONL output. Package the CLI in a GPU-enabled container from E-1, with locked Python dependencies, pytest for deterministic rule/contract checks and Ruff for code checks. Docker Compose runs AVA-owned components; shared infrastructure stays external (ADR-9).

### Reference host supplied by the owner

Snapshot: 2026-10-19 22:17:15, as supplied by the owner; not independently probed from this checkout.

| Component | Reported state |
|---|---|
| Host | `ai-host1`, Ubuntu-based Linux x86_64; distribution release still to capture from `/etc/os-release` |
| Kernel | `7.0.0-38-generic`, Ubuntu build `#38`, PREEMPT_DYNAMIC |
| System Python | `python3 --version`: **3.14.4** |
| NVIDIA driver / NVIDIA-SMI | **595.99.02** |
| CUDA shown by NVIDIA-SMI | **13.2**; driver-reported compatibility, not proof of installed toolkit or PyTorch runtime |
| GPU 0 | RTX 3090, 24,576 MiB; PCI bus `00000000:01:00.0`; 266 MiB used, including `python3.13` PID 3170 using 256 MiB |
| GPU 1 | RTX 3090, 24,576 MiB; PCI bus `00000000:03:00.0`; 23,744 MiB used, including `/app/llama-server` PID 2725 using 23,734 MiB |

RTX **30-series is the minimum hardware baseline** for this project, not the long-term hardware ceiling. The dual RTX 3090 system is the reference development/benchmark host; keep newer NVIDIA GPUs in scope and benchmark them under their own manifests. Series membership alone does not guarantee a model fits: VRAM, decoder features and measured capacity remain device-specific. Each reference GPU has its own 24 GB budget.

The current operating topology assigns one RTX 3090 to the continuously running
local-AI service and shares the other among AVA and other projects. The local-AI
path is LiteLLM → Qwen3.8 Dense 27B (144k context) → llama.cpp. Device ordinals,
process IDs and memory use above remain snapshot facts, not permanent identity.
Every AVA run checks UUID, allocation and competing load. Both GPUs may be
temporarily configured for an explicitly approved test, but separate VRAM is
never treated as a pooled 48 GB budget and running workloads are never stopped
implicitly.

### Current stable stack policy

Select the newest mutually compatible stable Python/PyTorch container stack; evaluate Python 3.14 first where stable wheels and model dependencies support it. The host's **Python 3.14.4** is inventory, not a requirement that the container use the same interpreter. Select current stable PyTorch/torchvision CUDA wheels, current stable Ultralytics and the current released YOLO/YOLOE families (YOLO26/YOLOE-26 candidates in ADR-3). Verify wheels, text-encoder dependencies, tracking and actual GPU inference together. If a component blocks Python 3.14, record the exact failure and use the newest mutually supported stable interpreter in the image; preserve the host's system Python. Python 3.11 is no longer the default.

Across the project, prefer the latest mutually compatible **stable** releases, including maintained SDKs/APIs and model checkpoints. Nightly, preview and unreleased branches are not defaults. Pin exact versions after the compatibility run, then upgrade deliberately with relevant regression checks. An older component requires a documented compatibility/quality reason and revisit trigger; do not retain old examples merely because the original ideas used them. The system driver's CUDA label does not require the PyTorch wheel to carry that same runtime version; verify the chosen wheel against official compatibility guidance and record `torch.version.cuda` separately.

### First execution workflow

AVA runs in containers on the physical AI host (`ai-host1`). Use Docker Engine/Compose with NVIDIA Container Toolkit, explicit GPU assignment and mounted input/output/model-cache paths. Private documentation is manually transferred and excluded from Git and image build contexts under ADR-10. Record the tested code commit, private documentation revision, image digest and host manifest.

Build one package and CLI with small modules for ingest, inference, tracking, rules, events and metrics. Keep adapters narrow: timestamped frame input; detection/track metadata output; rule-state updates; event sink. Do not design a plugin platform before a second backend needs it.

## Alternatives and consequences

Starting in DeepStream/C++ could improve throughput but delays the first visible outcome and complicates debugging. Building services/UI first provides little evidence about detection quality. A Python CLI makes intermediate outputs easy to inspect but CPU decode/copies are expected to limit scale; E-3 will replace the hot path while preserving contracts.

No database, broker, web framework, Kubernetes, training stack or generative model is required for the first demo. Run the two detectors sequentially for a comparable baseline.

## Validation and revisit

E-1 reproduced both detector paths on GPU 0 of `ai-host1` using the CUDA
container, Python 3.12.3, torch 2.14.1+cu130 and Ultralytics 8.4.183. Both
documented runs completed twice without first-use downloads; measured evidence
is in `benchmarks/e-1/report.md`. E-2 configuration and deterministic rules are
implemented, while its evidence and quality gates remain open.

The installed versions are recorded, but the build still uses dependency ranges
and an unpinned Git dependency; B-016 tracks the missing reproducible lock.
Revisit interpreter choices on compatibility failure and the backend when E-3
measures the Python limit.

References checked 2026-10-19: [PyTorch stable installation guidance](https://pytorch.org/get-started/locally/), [NVIDIA CUDA compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/), Ultralytics [tracking integration](https://docs.ultralytics.com/modes/track/) and [YOLOE](https://docs.ultralytics.com/models/yoloe/). The measured environment is a tested baseline, not yet a locked dependency resolution.
