# ADR-7: reproducible environments and staged operational controls

Date: 2026-10-19 · Revised: 2026-10-20 (containers and external services) · Status: Accepted for PoC; pilot controls proposed · Scope: all epics.

## Context and decision

AVA development has moved to the AI host. Run AVA in containers using Docker Engine, Docker Compose and NVIDIA Container Toolkit. The reference host snapshot and measured E-1 environment are in [ADR-1](ADR-1.md). Host drivers and existing workloads remain host-managed; Python/application/CUDA user-space dependencies belong in the selected image.

Follow ADR-1's current stable stack policy: select mutually compatible stable images and dependencies, lock packages and pin tested image digests. Container Python need not match the host interpreter. For E-3, use the current supported DeepStream container/runtime matrix with supported APIs. Verify actual host OS release, driver compatibility, container GPU visibility and decode capability; the kernel string alone does not establish SDK support.

Compose defines AVA-owned workers/API. The RTSP replay harness is a separate
AVA-owned Compose project so it can start, stop and be measured independently
of analytics workers. It uses one CPU-only MediaMTX container and one FFmpeg
publisher container per emulated camera, mounts existing videos read-only,
allocates no GPU and binds host access locally. For the current learning
environment, run the project-owned CPU-only operations stack selected by
[ADR-15](ADR-15.md) independently from both GPU workers and the RTSP harness.
It contains only services activated by current stories. External services
inventoried in [ADR-9](ADR-9.md) remain later integration targets; do not
provision or upgrade them from AVA. Configure endpoints and credentials through
private runtime configuration/secret mounts. Verify DNS/routing/authentication
from inside the application container. Use dedicated AVA namespaces and
least-privilege accounts; external service lifecycle and backups remain with
their existing operators.

Mount footage read-only, persist results/evidence/model caches explicitly, and grant only required GPU/device access. GPU compute visibility alone does not certify NVDEC support; E-3 tests decode in its image. Start/stop only AVA-owned components. Keep private docs outside image build contexts and runtime images under [ADR-10](ADR-10.md).

Reserve GPU UUIDs and execution windows explicitly. One RTX 3090 normally runs
the local-AI service and the other is shared by AVA and other projects. Approved
tests may temporarily configure both GPUs after a fresh allocation check and
explicit coordination of affected workloads. Do not stop workloads, expose
services publicly or upgrade shared drivers as incidental setup. Keep a CPU
smoke path where practical, clearly separated from GPU certification.

AVA reaches local model engines only through LiteLLM. The current implementation
is Qwen3.8 Dense 27B with a 144k context window served by llama.cpp; this is a
runtime manifest, not a domain dependency. Text is currently available. Qwen
vision and ComfyUI remain disabled/unintegrated candidates until a separately
approved experiment defines privacy, API and resource limits. AVA does not own
the lifecycle of LiteLLM, llama.cpp or ComfyUI.

Track third-party software, model/checkpoint and dataset licenses in the implementation manifest. Ultralytics offers AGPL/Enterprise options; determine whether the intended distribution/deployment terms are acceptable before proprietary integration, with appropriate owner/legal review if needed. A working model is not evidence that its intended use is licensed. Source: official [YOLO26 model/license overview](https://docs.ultralytics.com/models/yolo26/) (checked 2026-10-19); verify actual chosen assets' terms at adoption.

Use legally usable clips, keep raw footage/evidence/models/engines outside Git, and avoid committing camera credentials or personal footage to logs. Initial PoC is local/offline. Before E-6 networked access, define authenticated API/evidence access, transport protection, retention/cleanup, filesystem/service permissions and secret injection. SeaweedFS S3 is the target external evidence store; its endpoint/access/retention settings and the identity provider still need confirmation; do not build enterprise identity infrastructure in E-1.

## Alternatives and consequences

Unpinned 'latest' installations are convenient but make benchmark comparisons unreliable. A full production platform now would delay learning. This staged approach needs explicit pre-pilot reviews and may require replacing a dependency or rebuilding engines; record the cost rather than ignoring it.

## Validation and revisit

E-1 reproduces the environment on the actual host; E-3 verifies container/hardware compatibility and resource isolation. E-6 verifies access, cleanup, backup/restore and startup recovery. Revisit for commercial licensing restrictions, air-gapped/cloud deployment, sensitive retention requirements or multi-host availability.

References checked 2026-10-20: [Docker Compose GPU access](https://docs.docker.com/compose/how-tos/gpu-support/), [NVIDIA Container Toolkit installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). No host installations or service connections were performed.
