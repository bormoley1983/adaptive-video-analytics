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
| --- | ------ | -------- | ---------- |
| 1 | Box matching + accuracy review | **Done** (directional) | `t1-timing-summary.txt`; IoU matching in Gate 1; no labelled ground truth for absolute mAP |
| 2 | Throughput/timing measurement | **Done** | `t1-timing-summary.txt`: 415.8 FPS wall, 5.2× vs E-2 |
| 3 | YOLOE export + class-change behavior | **Done** | `gate3-yoloe-findings.md`; `t1-yoloe-detections.jsonl` (1768 frames) |
| 4 | Device isolation policy + go/no-go | **Done** | `gate4-device-isolation-and-verdict.md`: GO with conditions |

All four T1 gates are complete. The compatibility verdict is **GO — with
conditions** (see Gate 4 document). Remaining open items for full T1 closure:
owner sign-off on the directional accuracy comparison, and ADR-2 status update.

RTSP, NvDCF/rule/event parity, overload and 30-minute multi-stream capacity are
not established by this conventional one-stream experiment.

---

# E-3-T5: measured single-GPU multi-stream capacity sweep

Status: **Sweep complete (16/16 points ran to completion); no point meets the
4 FPS/camera pass gate.** Capacity is therefore **provisional**, not a passing
limit. The dominant finding is that per-camera analysis throughput is capped just
below the configured `analysis_fps: 4.0` target at every stream count, so the
measured "capacity" is set by the frame-rate decimation + bounded-buffer design,
not by GPU headroom. See [Root cause of the sub-4 FPS window](#root-cause-of-the-sub-4-fps-window)
and the parallel-investigation handoff at
[`docs/handoff-e3-t5-fps-cap.md`](../../docs/handoff-e3-t5-fps-cap.md).

## Scope and method

- **Workload:** live multi-stream RTSP analytics on the T3 opencv/gstreamer
  adapter (`src/ava/live_runner.py`), GPU 0 only (`CUDA_VISIBLE_DEVICES=0`).
  GPU 1 (shared llama-server) was never touched.
- **Points:** 4 / 16 / 25 / 50 cameras × {conventional, prompted} ×
  {shared, per_camera} detector mode = **16 points**.
- **Duration:** each point ran `duration_s: 2100` (35 min) steady-state after
  warm-up, exceeding the 30-minute minimum in the E-3 completion gate.
- **Footage:** 50 normalized H.264 clips (1280×720, 10 FPS source), looped by
  FFmpeg publishers into MediaMTX; each camera consumed at its native ~10 FPS.
- **Detector profiles:** conventional = YOLO26n + ByteTrack; prompted =
  YOLOE-26s-seg + MobileCLIP text encoder (prompts: person, car, motorcycle).
- **Bring-up:** `./harness/stack.sh up --no-ops` (self-healing, health-gated);
  stream health confirmed with `./harness/probe.sh`.

## Completion-gate verdict per point

The E-3 completion gate for a *passing* capacity point requires, over the
30-minute run: each camera sustains **≥ 4 analysis FPS**, primary-event p95
latency **< 3 s** (excluding dwell), queues do not grow, and no worker crashes.

| Gate | Result across all 16 points |
| --- | --- |
| ≥ 4 analysis FPS per camera | **FAIL everywhere.** Measured 2.88–3.32 FPS (see table). |
| Event p95 latency < 3 s | PASS where events exist (worst 0.33 s at 50-conv-per_camera). |
| Queues do not grow | Plateau at depth 40 (= `buffer_size`), age ~10 s; bounded, not unbounded growth. |
| No worker crashes | PASS — all 16 points reached EOS with exit 0. |

**Highest passing point: none.** Every point is provisional because the FPS gate
is unmet at every stream count. **First bottleneck:** per-camera analysis
throughput capped near ~3.3 FPS (below the 4.0 target) with a constant ~50%
buffer-full drop rate — independent of GPU, which stays far from saturated.

## Detailed per-point results

All values are per-point aggregates over the 2100 s run. `afps min/avg` is the
minimum and mean analyzed FPS across that point's cameras. `drop%` is the
buffer-full drop rate (dropped / (decoded + dropped)). `q_end` is the end-of-run
mean queue depth. `lat_p95s` is the worst-camera event-latency p95 (0 where no
events were emitted). GPU/CPU/RAM are host-sampler averages/peaks over the run.

| point | cams | kind | mode | wall_s | in_fps | afps min/avg | drop% | q_end | lat_p95s | evts | gpuU avg/peak% | gpuMemPeak MiB | decU% | load1m | ramAvail MiB | inferP95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4-conv-shared | 4 | conv | shared | 2100 | 9.95 | 3.31/3.32 | 50.0 | 40 | 0.00 | 0 | 2.9/8 | 1144 | 0.0 | 0.68 | 9344 | 7.0 |
| 4-conv-per_camera | 4 | conv | per_camera | 2100 | 9.96 | 3.31/3.32 | 50.0 | 40 | 0.00 | 0 | 2.7/8 | 1218 | 0.0 | 0.61 | 9153 | 7.4 |
| 4-prom-shared | 4 | prom | shared | 2100 | 9.96 | 3.31/3.32 | 50.0 | 40 | 0.00 | 0 | 5.3/12 | 1416 | 0.0 | 0.49 | 8787 | 8.0 |
| 4-prom-per_camera | 4 | prom | per_camera | 2100 | 9.96 | 3.31/3.32 | 50.0 | 40 | 0.00 | 0 | 5.1/38 | 1784 | 0.0 | 0.67 | 9940 | 15.4 |
| 16-conv-shared | 16 | conv | shared | 2100 | 9.82 | 3.22/3.27 | 50.0 | 40 | 0.01 | 17 | 11.5/23 | 1942 | 0.0 | 1.16 | 9425 | 13.3 |
| 16-conv-per_camera | 16 | conv | per_camera | 2100 | 9.83 | 3.23/3.28 | 50.0 | 40 | 0.00 | 0 | 11.6/22 | 2398 | 0.0 | 1.22 | 7705 | 10.8 |
| 16-prom-shared | 16 | prom | shared | 2100 | 9.83 | 3.23/3.28 | 50.0 | 40 | 0.00 | 0 | 19.7/31 | 2214 | 0.0 | 1.39 | 6612 | 14.8 |
| 16-prom-per_camera | 16 | prom | per_camera | 2100 | 9.84 | 3.23/3.28 | 50.0 | 40 | 0.00 | 0 | 20.2/36 | 3984 | 0.0 | 1.65 | 6350 | 15.9 |
| 25-conv-shared | 25 | conv | shared | 2100 | 9.73 | 3.16/3.24 | 50.0 | 40 | 0.01 | 31 | 17.6/24 | 2560 | 0.0 | 1.87 | 5277 | 11.6 |
| 25-conv-per_camera | 25 | conv | per_camera | 2100 | 9.75 | 3.17/3.25 | 50.0 | 40 | 0.00 | 0 | 18.0/26 | 3029 | 0.0 | 2.84 | 5715 | 18.0 |
| 25-prom-shared | 25 | prom | shared | 2100 | 9.73 | 3.16/3.24 | 50.0 | 40 | 0.00 | 0 | 31.1/48 | 2551 | 0.0 | 2.43 | 5474 | 22.6 |
| 25-prom-per_camera | 25 | prom | per_camera | 2100 | 9.74 | 3.17/3.25 | 50.0 | 40 | 0.00 | 0 | 31.3/57 | 5813 | 0.0 | 2.63 | 5261 | 29.9 |
| 50-conv-shared | 50 | conv | shared | 2100 | 9.49 | 3.00/3.16 | 49.9 | 40 | 0.23 | 73 | 34.5/42 | 3941 | 0.0 | 4.11 | 2401 | 234.9 |
| 50-conv-per_camera | 50 | conv | per_camera | 2100 | 9.09 | 2.88/3.03 | 49.9 | 40 | 0.33 | 33 | 35.8/48 | 5545 | 0.0 | 4.35 | 2562 | 345.0 |
| 50-prom-shared | 50 | prom | shared | 2100 | 9.47 | 3.00/3.16 | 49.9 | 40 | 0.00 | 0 | 60.6/71 | 4213 | 0.0 | 3.57 | 3839 | 64.5 |
| 50-prom-per_camera | 50 | prom | per_camera | 2100 | 9.25 | 2.96/3.08 | 49.9 | 40 | 0.00 | 0 | 63.9/78 | 11001 | 0.0 | 4.98 | 3237 | 327.3 |

### Reading the table

- **GPU is not the constraint.** Utilization averages 2.9%→63.9% and peaks at
  78%; VRAM peaks at 11 GB (of 24 GB) only at the heaviest prompted point. There
  is large headroom, yet FPS still sits below target — so the cap is upstream of
  GPU saturation.
- **`decU%` (decoder util) reads 0.0** because the opencv/gstreamer adapter does
  not expose a hardware-decoder utilization counter; this is a telemetry gap, not
  evidence that decoding was idle. Decoding clearly ran at ~10 FPS/camera
  (`in_fps`).
- **`inferP95 ms` spikes at 50 cameras** (up to 345 ms) — the shared detector
  lock serializes inference across all cameras, so per-call latency grows with
  camera count. This is the first *real* scaling pressure visible in the data,
  but it still does not push average FPS below ~2.9.
- **shared vs per_camera:** near-identical analyzed FPS; per_camera uses more
  VRAM (one model instance per camera) and shows higher GPU-util peaks.

## Root cause of the sub-4 FPS window

The measured analyzed FPS is *by construction* just under the configured
`analysis_fps: 4.0`. The mechanism, from `src/ava/live_runner.py`:

1. **Decode runs at native rate** (~10 FPS/camera) and every decoded frame is
   pushed into a bounded buffer (`deque(maxlen=buffer_size)` with
   `buffer_size: 40`).
2. **Inference is decimated to `analysis_fps`** using the *source PTS*: a frame
   is inferred only when `frame.timestamp_s - last_infer_ts >= 1/analysis_fps`
   (i.e. ≥ 0.25 s). This targets exactly 4 inferences/s per camera.
3. **The buffer fills and overflows.** Because decode (~10 FPS) outpaces the
   decimated inference (~4 FPS), the 40-deep buffer stays full; every frame that
   arrives while it is full is counted as a `buffer_full` drop. At a steady
   10-in / 4-out ratio this yields the observed **~50% drop rate** and a queue
   depth pinned at exactly `buffer_size` (40), with age ≈ 40/4 = 10 s.

So the ~3.3 FPS is not a GPU or host limit — it is the decimation target minus
timing overhead:

- Target inferences over 2100 s at 4.0 FPS = **8400**. Measured ≈ **6990**
  (cam-01, 4/25-cam points) → a **~17% shortfall**, giving ~3.33 FPS.
- The shortfall comes from PTS-based gating on a 10 FPS source: with frames
  arriving every 0.1 s and a 0.25 s gate, the realized interval is quantized to
  0.3 s (3 inferences per 0.9 s of source time) rather than exactly 0.25 s, plus
  small warm-up/teardown loss at run boundaries. At 50 cameras the shortfall
  widens (~21%) as decode rate dips to ~9.1–9.5 FPS under host load.

**Consequence for the capacity claim:** the sweep measured a *throughput ceiling
imposed by the analysis_fps decimation + bounded-buffer design*, not the true
GPU/host capacity limit. To measure real capacity, the decimation target and
buffer must be raised (or removed) so inference is no longer self-limited below
the point where GPU or host resources actually saturate. This is handed off for
parallel investigation.

## Observability-stack overhead

The ops stack (Prometheus v3.15.1, Grafana v13.2.4, Loki v3.7.9, Alloy v1.21.2)
ran alongside the sweep on CPU only. Per B-022's measured baseline the full ops
project costs **~486 MiB RAM and < 2% CPU** at idle; during the sweep the host
`load1m` rose from ~0.5 (4 cams) to ~5.0 (50 cams), dominated by the analytics
workers, not the observability stack. The per-point `ramAvail` decline
(9344 → 2401 MiB at 50-conv-shared) tracks camera count and is attributable to
the live runner's per-camera buffers/decoders, with the ops stack contributing a
small constant offset.

## Quality comparison with E-2

E-2 (`benchmarks/e-2/report.md`) is a **functional** baseline, not an accuracy
certification: 16,529 detections / 267 tracks over the single E-1 crosswalk clip
(1768 frames @ 59.94 FPS), with one null track_id (0.01%). The T5 sweep is a
*capacity* measurement over looped 720p/10 FPS clips and does **not** reproduce
E-2's detection/tracking counts, so no per-class or per-box parity is claimed.
Directionally:

- Conventional T5 points emitted events only where the rule geometry matched the
  looped footage (e.g. 73 events at 50-conv-shared); most prompted points emitted
  0 because the prompts/geometry did not trigger on the normalized clips. Event
  counts here are a liveness signal, not a quality metric.
- The T1 DeepStream run produced 16,848 detections (+319 / +1.93% vs E-2's
  16,529) — a directional count delta only, with no labelled ground truth for
  absolute accuracy. That comparison is unchanged by T5.

A true quality comparison (detection/tracking parity between the live opencv
adapter and the E-2 offline pipeline) remains open and is out of scope for this
capacity sweep.

## Reproducibility

| Item | Value |
| --- | --- |
| Task branch | `agent/e3-t5-capacity-sweep` (worktree `../ava-worktrees/e3-t5-capacity-sweep`) |
| GPU | RTX 3090 GPU 0 only; `CUDA_VISIBLE_DEVICES=0`; GPU 1 (llama-server) untouched |
| Sweep driver | `harness/run_capacity_sweep.sh` (`START_INDEX` resume supported) |
| Bring-up | `./harness/stack.sh up --no-ops`; health via `./harness/stack.sh status`, `./harness/probe.sh` |
| Per-point configs | `configs/run-e3-cap-{4,16,25,50}-{conv,prom}-{shared,per_camera}.yaml` |
| Status file | `results/e3-capacity-sweep/sweep-status.tsv` (16 PASS rows) |
| Per-point metrics | `results/<point>/metrics.json`, `events.jsonl`, `run.log` |
| Metric extractors | `results/e3-capacity-sweep/extract_metrics.py`, `extract_full.py` |

## T5 gate status

| # | Gate | Status | Evidence |
| --- | ------ | -------- | ---------- |
| 1 | Run 4/16/25/50 × conv/prom × shared/per_camera, ≥30 min each | **Done** | 16/16 PASS, wall ≈ 2100 s each (`sweep-status.tsv`) |
| 2 | Report highest passing point | **None passes** — all provisional (FPS gate unmet) | per-point table above |
| 3 | Report first failure/bottleneck | **Done** — sub-4 FPS decimation cap + ~50% buffer-full drops | Root-cause section |
| 4 | Report observability-stack overhead | **Done** — ~486 MiB / <2% CPU (B-022 baseline) | Overhead section |
| 5 | Quality comparison with E-2 | **Directional only** — no parity claimed | Quality section |

Open: raise/remove the `analysis_fps` decimation + bounded-buffer self-limit and
re-run to find the true GPU/host saturation point (see handoff).
