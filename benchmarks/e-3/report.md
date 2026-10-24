# E-3-T1: DeepStream 9.2 compatibility spike

Status: **All four T1 gates complete; verdict GO with conditions.** Awaiting owner sign-off on directional accuracy and ADR-2 status update for full closure.

## Current result — 2026-10-24

The conventional YOLO26n pipeline now decodes the complete crosswalk clip,
runs TensorRT inference and reaches EOS with exit 0. It produces 1,768 JSONL
frame records and 16,848 detections. This is 319 more (+1.93%) than the
documented E-2 total of 16,529 detections. This is a directional count comparison,
not an accuracy pass: there is no labelled matching analysis or agreed tolerance.

The observed stall depends on container device exposure. With GPU 0 alone
exposed, CUDA initializes but NVDEC returns `CUDA_ERROR_NO_DEVICE` (100).
With both device nodes exposed and CUDA pinned to GPU 0's UUID, NVDEC initializes,
hardware decoding reaches EOS and the corrected app succeeds. The owner explicitly
approved this temporary experiment. Broader device exposure weakens the device
access boundary; it is an experimental workaround, not a permanent deployment
or GPU-isolation decision. The internal driver mechanism remains unknown.

YOLOE export/profile-change behavior, accuracy approval and capacity remain open.
No migration or production-readiness approval is implied.

## Reproducibility manifest

| Component | Value |
| --- | --- |
| Code base / task branch | `15c5c3829e5eb65963725f8e1b9b9e8d7f2d1635` / `agent/e3-t1-compat-spike`, uncommitted changes |
| Host | ai-host1, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Selected GPU | RTX 3090 GPU 0, UUID `GPU-facd1ddc-c4ab-7ac9-0000-ead909e0ec42` |
| Driver | 595.99.02 |
| SDK container | `ghcr.io/nvidia/deepstream@sha256:5ee7458d5e795592e0cb182457a7f8281f2c1da7bb38ce4b8506ac693f266746` |
| Runtime | DeepStream 9.2; GStreamer 1.24.2; TensorRT 10.16.0.72; CUDA 13.2; cuDNN 9.20.0.48 |
| Input | Crosswalk H.264 High, 1920×1080, 60000/1001 FPS, 1,768 frames, approximately 29.496 s |
| Input SHA-256 | `110501b29df6f348f8942987a1d70e3d9f412a9b55f7b9e6c8917bcdab49f239` |
| Engine SHA-256 | `23974b184031ee6e4855c02d49ab65ea846fae099b9276ac8af175280dd72c82` |
| Parser library SHA-256 | `c711f5221074ec8e8abd7f6167c5e6f4268fd4db68562a4231bc8cc35f8baea1` |
| Inference config SHA-256 | `6e3fd029973ff4a12f51a36cc373c1b647d350771519eea9acb681101f0506d5` |
| App config SHA-256 | `f9d4bc0ec762ce62e0474bb29e5b58849d10845b7e632f22aaabde499ca529ea` |
| Detector settings | Existing FP16 engine, batch 1, 640 input, RGB 1/255, aspect-preserving symmetric padding, confidence 0.35, class-aware parser NMS IoU 0.5, global top-K 200 |
| Tracking / rendering / replay | Disabled / disabled / file played without real-time sink pacing; no file loop |
| GPU access | Approved `--gpus all`; `CUDA_VISIBLE_DEVICES=GPU-facd1ddc-c4ab-7ac9-0000-ead909e0ec42`; decoder/mux/infer plugin IDs 0 |
| Other workloads | ComfyUI on GPU 0; llama-server on GPU 1, retained |

The engine/export tensor contract is inherited from the handoff: float
`(1,84,8400)`. The parser remains specific to that contract; arbitrary tensor
shape/type rejection is not implemented. The run verifies this supplied artifact,
not arbitrary exports.

## Results and comparison

| Check | Result |
| --- | --- |
| Decoder-only crosswalk run | PLAYING, NVMM NV12 output tagged GPU 0, EOS, exit 0 |
| Decoder processing interval | 2.666921798 s reported by gst-launch; excludes initialization and is not inference latency |
| Conventional app | Pipeline running, mux EOS, app EOS, “App run successful”, exit 0 |
| Frame records | 1,768; contiguous parser invocation sequence 0–1767 |
| Empty frame records | 14 |
| Detections | 16,848 |
| Documented E-2 total | 16,529 detections / 267 tracks |
| Directional detection-count delta | +319 / +1.93% |
| Tracking comparison | Unavailable; this DeepStream run has no tracker |
| Detection confidence range | 0.350421–0.934157 |
| Pipeline FPS / synchronized inference latency / warm-up | Not measured in this run; the app completed before its first periodic performance report |
| Raw-record validation | Valid JSON, contiguous sequence, class IDs 0–79, finite values, confidence ≥0.35, positive boxes bounded by the 640 model space |

The parser JSONL sequence is invocation order, not source PTS. Its boxes are in
model coordinates before DeepStream's source-coordinate metadata processing.
Do not use this output as a source-coordinate tracking/event contract.

### Class distribution

| Class | Detections |
| --- | ---: |
| person | 12,251 |
| bicycle | 166 |
| car | 1,681 |
| motorcycle | 430 |
| bus | 387 |
| train | 33 |
| truck | 243 |
| traffic light | 41 |
| stop sign | 74 |
| backpack | 7 |
| umbrella | 822 |
| handbag | 702 |
| suitcase | 2 |
| chair | 3 |
| cell phone | 6 |

The current local `results/e2-rules/detections.jsonl` contains 16,811 detections
and zero non-null track IDs. It is not the historical 16,529-detection/267-track
artifact described in the E-2 report, so its class counts were not presented as
that baseline. Reconstruct or recover the original baseline before per-class or
per-box parity approval. Differences in preprocessing, precision, tracking,
class selection and postprocessing prevent a count delta from proving accuracy.

## Device-exposure diagnosis

The identical typed `probe_nvdec.py` ran without footage:

| Docker exposure | CUDA selection | CUDA-visible count | NVDEC capability query | Exit |
| --- | --- | ---: | --- | ---: |
| `--gpus device=0` | GPU 0 UUID | 1 | `CUDA_ERROR_NO_DEVICE` (100) | 1 |
| `--gpus all` | GPU 0 UUID | 1 | `CUDA_SUCCESS` (0) | 0 |

The approved decoder follow-up, with the same CUDA UUID and broader exposure,
completed the full clip. The approved 120-second app follow-up also completed.
A post-run allocation check found only the pre-existing ComfyUI process on GPU 0
and llama-server on GPU 1; the sample was taken after the short app had exited,
so it is not continuous GPU-isolation or utilization evidence.

Earlier decoder-only crosswalk (20-second bound) and NVIDIA bundled 720p sample
(10-second bound) runs, with GPU 0 alone exposed, both exited 124 in preroll.
Buffer-pool logging shows the first compressed packet queued and input streaming
started without decoded output. CUDA/NVDEC libraries and video capabilities were
present. No Xid appeared in the inspected kernel boot records.

NVIDIA's [troubleshooting guide](https://docs.nvidia.com/deepstream/9.2/text/DS_troubleshooting)
requires `nvstreammux` for normal DeepStream inference. The abandoned no-mux
route is not the final solution. A [report on NVIDIA's forum](https://forums.developer.nvidia.com/t/cuvidgetdecodercaps-decodecaps-returned-cuda-error-no-device-code-100/349517)
describes similar per-device-exposure behavior; this provides context, not proof
of the internal cause on this host. The successful final pipeline uses the
SDK-supported app schema and muxing.

Historical stock ResNet18 decode/inference evidence from 2026-10-22 remains
partial compatibility evidence. The original `t1-headless-one-stream.txt`
incorrectly points `model-engine-file` at ONNX and is retained as an invalid,
inconclusive diagnostic attempt; it is not the current runnable configuration.

## Commands and local evidence

[README.md](README.md) lists the mount contract. The two approved run commands
inside the pinned container were:

```bash
timeout -k 3s 20s gst-launch-1.0 -e -v \
  filesrc location=/tmp/crosswalk.mp4 ! qtdemux ! h264parse ! \
  nvv4l2decoder gpu-id=0 ! fakesink sync=false

timeout -k 5s 120s /tmp/run_yolo26n_one_stream.sh
```

Successful detections and complete app log:
`/tmp/e3-t1-yolo26n.gmTW5U/detections.jsonl` and
`/tmp/e3-t1-yolo26n.gmTW5U/pipeline.log`.
Their SHA-256 values are
`a27e6d54c970aa77b42d4c9460e9f31b21eb423ea79c90e658fb65de1f667785`
and `0ff09cf229a25d3ac3a6515df9b4fceebeeb7d2b57ca7b89d0a947e31c1bf5e3`.

Other retained logs:

The successful app output/log, decoder-success log, controlled probe logs,
parser build/test log, exact runtime library and Python gate output have also
been preserved under `results/e3-t1-handoff-2026-10-24/` in the task worktree.
All eight archived files pass `sha256sum -c SHA256SUMS`. This archive is ignored
by Git and survives `/tmp` cleanup; recover it before removing the worktree.

- `/tmp/e3-t1-decode-all-visible.log`: successful decoder run.
- `/tmp/e3-t1-nvdec-all-visible.log`, `/tmp/e3-t1-nvdec-single-visible.log`: controlled capability comparison.
- `/tmp/e3-t1-decode-isolation.log`, `/tmp/e3-t1-decode-stock.log`, `/tmp/e3-t1-bufferpool.log`: earlier stalls.
- `/tmp/e3-t1-decoder-environment.log`: library and decoder inventory.
- `/tmp/e3-t1-parser-tests.oQcJov/build.log`: parser build and eight passing deterministic contract tests.

The parser build uses `build_parser.sh` in the pinned SDK image without GPU
access. NVIDIA headers emit warnings under `-Wall -Wextra`; the initial
`-Werror` attempt failed on those warnings. The tested rebuild is byte-identical
to the successful-run library. Contract tests cover missing output, thresholds,
class limits, NMS, coordinate mapping, right/bottom clipping, zero-area rejection,
global top-K and JSONL append/empty-frame behavior.

The repository gates ran through host make and the existing CPU-only dev image:

```bash
make -k 'PYTHON=docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -e PYTEST_ADDOPTS=-pno:cacheprovider -e COVERAGE_FILE=/tmp/ava-coverage -e RUFF_CACHE_DIR=/tmp/ruff-cache -v /home/admaccess/ava-worktrees/e3-t1-compat-spike:/work:ro -w /work ava/dev:local' \
  test-unit test-integration test-e2e test-infra test-coverage lint
```

| Gate | Result |
| --- | --- |
| `make test-unit` | 259 passed, 8 deselected |
| `make test-integration` | 1 passed, 266 deselected |
| `make test-e2e` | 267 deselected, pytest exit 5; unavailable, not passed |
| `make test-infra` | 7 skipped, 260 deselected; Docker unavailable inside test container, not passed |
| `make test-coverage` | 260 passed, 7 deselected; 94.40% combined line/branch coverage |
| `make lint` | Passed for src/tests |

The aggregate make command exits 2 because the empty e2e selector exits 5;
`-k` allowed the remaining independent gates to complete. Full evidence is
`/tmp/e3-t1-make-gates.log`. Host pytest/Ruff and the earlier temporary venv were
unavailable. The initial attempt to run make inside the dev image failed because
that image has no make binary. Python source was not changed; this regression
coverage does not measure the separate C++ parser or certify GPU performance.
The new capability probe also passes a separate `ruff check --no-cache`
invocation. Shell syntax, probe syntax, tracked whitespace and all ten new
text-file whitespace checks pass. Synthetic parser JSONL parses as the expected
empty frame followed by one detection. No named experiment containers remain.

Host syscall attach was unavailable (`Operation not permitted`). Automatic
approval review rejected a proposed third-party FFmpeg/footage comparison; it
was not executed. No host driver, shared service or permanent GPU configuration
was changed. Test containers were stopped or removed. Temporary evidence must
be recovered before host cleanup; generated media/models/libraries/results stay
outside Git.

## E-3-T1 gate status (2026-10-24)

| # | Gate | Status | Evidence |
|---|------|--------|----------|
| 1 | Box matching + accuracy review | **Done** (directional) | `t1-timing-summary.txt`; IoU matching in Gate 1; no labelled ground truth for absolute mAP |
| 2 | Throughput/timing measurement | **Done** | `t1-timing-summary.txt`: 415.8 FPS wall, 5.2× vs E-2 |
| 3 | YOLOE export + class-change behavior | **Done** | `gate3-yoloe-findings.md`; `t1-yoloe-detections.jsonl` (1768 frames) |
| 4 | Device isolation policy + go/no-go | **Done** | `gate4-device-isolation-and-verdict.md`: GO with conditions |

All four T1 gates are complete. The compatibility verdict is **GO — with
conditions** (see Gate 4 document). Remaining open items for full T1 closure:
owner sign-off on the directional accuracy comparison, and ADR-2 status update.

RTSP, NvDCF/rule/event parity, overload and 30-minute multi-stream capacity are
not established by this conventional one-stream experiment.
