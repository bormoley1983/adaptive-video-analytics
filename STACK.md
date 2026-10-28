# AVA technology stack

Layered view of the technologies, their roles and the versioning specification.
The inventory below records the current profiles; executable pins remain in
the dependency lock, Dockerfiles and Compose files. Review this document at
every stack change and retain the validation status of each profile.

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
  tokenizer, on the CUDA 12.6 runtime image. Why: the tested E-1 reference path;
  `requirements.lock.txt` records its Python dependency closure. Container and
  framework CUDA versions are recorded separately below.
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

## Versioning specification

Inventory checked **2026-10-27** against repository pins and existing image
package metadata/version commands. These are recorded versions, not a claim
that they are the newest releases or a universal compatibility matrix.

### Runtime and build profiles

| Component | Python reference profile | DeepStream diagnostic profile |
| --- | --- | --- |
| Container base | CUDA `12.6.3-cudnn-runtime-ubuntu24.04` | DeepStream `9.2-samples-multiarch`, Ubuntu 24.04 |
| Python | 3.12.3 observed | 3.12.3 observed |
| PyTorch / torchvision | 2.14.1 / 0.29.1 locked | Outside this profile |
| Framework CUDA build | cu130; `torch.version.cuda = 13.0` observed | TensorRT/native CUDA path |
| CUDA runtime package | `nvidia-cuda-runtime==13.0.96` | `cuda-cudart-13-2=13.2.51-1` |
| CUDA compiler | Outside this runtime profile | nvcc 13.2.51 in the derived build image |
| cuDNN package | `nvidia-cudnn-cu13==9.24.0.43` locked | `libcudnn9-cuda-13=9.27.0.42-1` observed |
| TensorRT | Outside this profile | 10.16.1.11 Python/native packages observed |
| GStreamer | Record the actual selected capture backend per run | 1.24.2 observed |
| Ultralytics | 8.4.183 locked | Outside this profile |
| OpenCV | Wheel packages 5.0.0.93; `cv2` API reports 5.0.0 | CPU reference comparison runs separately |
| NumPy | 2.5.4 locked | Absent from the inspected SDK image; comparison runs separately |
| Pydantic / PyYAML | 2.14.1 / 6.0.3 locked | Outside this profile |

The SDK base identity is
`ghcr.io/nvidia/deepstream@sha256:5ee7458d5e795592e0cb182457a7f8281f2c1da7bb38ce4b8506ac693f266746`.
Its isolated build derivative adds CUDA compiler/development packages
13.2.51-1 with CCCL 13.2.27-1 and GCC 13.3.0; existing NVTX headers are
13.2.20-1. The local build image and generated libraries have their own
identities in the private build manifest. An SDK base digest alone does not
identify a derived image or a bind-mounted custom library.

CUDA **13.2.51 applies to the SDK/runtime and native build profile**. The
application still uses the cu130 framework dependency set on its CUDA 12.6.3
base. Record the driver, container runtime, compiler and framework build as
separate fields; the `CUDA Version` displayed by `nvidia-smi` describes driver
capability, not the installed compiler or the CUDA libraries loaded by PyTorch.

Observed host tooling: Docker client/server 29.9.2, Compose 5.6.1, NVIDIA
Container Toolkit CLI/library 1.20.1 and NVIDIA driver 595.99.02. The host nvcc
reports 12.4.131; native SDK compilation uses the container's 13.2.51 compiler.
These are observations, not minimum supported versions or approval to change
the shared host.

Validation status is profile-specific. The Python reference has measured E-1
detector evidence. The SDK has build, engine-binding, decode/inference-flow and
tensor-capture evidence, but square preprocessing still differs from the CPU
reference in the scaling stage; quality, full-pipeline and capacity gates remain
open. An older [E-3 report](benchmarks/e-3/report.md) lists TensorRT 10.16.0.72
and cuDNN 9.20.0.48, differing from the current pinned-image package queries.
Historical version reconciliation remains open; preserve the original report
and use per-run manifests when comparing results.

### Media and observability versions

Versions below were read from the binaries in the exact digest-pinned images.
The authoritative image references are in [replay Compose](harness/docker-compose.yml)
and [observability Compose](ops/docker-compose.yml); tags such as `latest` in a
local image cache are not the deployment identity.

| Component | Observed version | Pin source |
| --- | --- | --- |
| MediaMTX | 1.21.2 | Replay Compose |
| FFmpeg publisher image | 9.0.1 | Replay Compose |
| Prometheus | 3.15.1 | Observability Compose |
| Grafana | 13.2.4 | Observability Compose |
| Loki | 3.7.9 | Observability Compose |
| Alloy | 1.21.2 | Observability Compose |

The FFmpeg row identifies the publisher image; record an application
container's FFmpeg build separately. Inherited OCI version labels can describe
the base OS rather than the executable, so verify the component itself.

### Identity and authoritative pins

Keep upstream component version identifiers, including package revisions and
framework build variants. Identify an AVA stack configuration by its Git
revision, profile and resolved image/artifact manifest. Record a patch/source
hash for a dirty diagnostic worktree. A documentation date or image tag alone
is insufficient to reproduce a run.

| Artifact | Required identity and source |
| --- | --- |
| Python dependencies | Exact transitive versions in [requirements.lock.txt](requirements.lock.txt); direct dependencies in [pyproject.toml](pyproject.toml). VCS dependencies use a full commit, as CLIP does. |
| Container images | Repository plus full `@sha256:` digest and OS/CPU platform in Dockerfiles/Compose or the resolved manifest. Record derived image identity, base digest and exact added OS packages. A local-only image ID must be marked local-only. |
| Native parsers/preprocessors | Source revision/hash, binary SHA-256, SDK/TensorRT headers, compiler/CUDA versions, build flags and target GPU architecture. Record mounted libraries independently of the image. |
| Models and engines | Model/profile identifier, source-weight and export hashes, exporter/version/settings, engine hash, TensorRT/build GPU identity, batch/shape profiles and optimization precision. Record actual input/output names, dtypes and layout separately from optimization precision. |
| Runtime configuration | Config hash, class/prompt ordering, preprocessing dimensions/resize/padding/channel order/normalization, parser/NMS and tracker/rule settings. |
| Validation evidence | Profile/revision, immutable inputs, host/container/toolchain manifest, commands/results, test level, date, acceptance criteria and passed/failed/not-run status. |

Private model assets, footage, credentials and raw results remain outside Git
and image build contexts; keep their detailed manifests with local evidence.
Commit sanitized identifiers/configuration examples only.

Current reproducibility gaps remain explicit: the [application Dockerfile](Dockerfile)
uses a base tag, [application Compose](docker-compose.yml) uses `ava/e1:local`,
OS/pip-bootstrap installs are not fully locked, and development tools still
have version ranges. The Python lock and the digest-pinned replay/ops services
do not close those other gaps. Capture resolved identities now and tighten
executable pins through a separately validated change; this specification does
not modify them.

### Upgrade, acceptance and rollback

1. Select mutually compatible stable releases under [ADR-1](docs/architecture/adr/ADR-1.md)
   and [ADR-2](docs/architecture/adr/ADR-2.md). Document any older-version
   exception and revisit trigger. Check vendor support for the target driver,
   GPU architecture, framework, CUDA, SDK and TensorRT combination.
2. Build a candidate in an isolated task worktree/image. Update executable pins
   and this inventory together, record the resolved manifest, and preserve the
   previous accepted profile. Mark the candidate provisional until its gates pass.
3. Run the affected `make test-unit`, `make test-integration`, `make test-coverage`
   and `make lint` gates under [test rules](docs/tests_rules.md). Validate public
   configuration and tensor/parser contracts before runtime work.
4. Run affected `make test-infra` and `make test-e2e` checks with explicit runtime
   authorization and shared-GPU allocation checks. For media/model changes,
   compare pixels/tensors, geometry, detections, tracking/rules/events and the
   applicable latency/drop/capacity measurements. Use declared owner-approved
   quality tolerances; compilation or CPU doubles cannot establish GPU parity.
5. Treat a patch upgrade as a candidate too. Accept only the validation level
   actually demonstrated and follow the repository's human commit/MR/merge
   gates before promotion. Keep historical evidence tied to its original manifest.
6. Roll back to the previous image, locks, configuration and matching model/
   engine/parser set under the approved operational plan. Rebuild or revalidate
   engines when compatibility requires it; missing source weights/export
   provenance block a reproducibility claim. Do not reuse an engine across a
   changed environment merely because its filename is unchanged.
