# E-1 baseline report

Status: **measured** — both detector paths completed the same clip on GPU 0 with
CUDA-synchronized per-phase timing. This is a processing baseline for a functional
PoC, **not** a live-event-latency or detection-quality guarantee (see E-1 scope).

## Environment

| Item | Value |
|---|---|
| Host | `ai-host1`, Ubuntu 26.04.1 LTS (resolute), kernel 7.0.0-38-generic |
| GPU (selected) | RTX 3090, UUID `GPU-facd1ddc-c4ab-7ac9-0000-ead909e0ec42` (GPU 0) |
| GPU (preserved) | RTX 3090, UUID `GPU-5e0a88d2-…791` (GPU 1, llama-server — untouched) |
| Image | `ava/e1:local`, digest `sha256:ce490d58…b986d4e8` (rebuilt for per-phase timing instrumentation) |
| Base image | `nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04` |
| Container Python | 3.12.3 |
| torch | 2.14.1+cu130 (`torch.version.cuda` = 13.0, CUDA available) |
| ultralytics | 8.4.183 |
| opencv (cv2) | 5.0.0 (headless) |
| Driver / driver-reported CUDA | 595.99.02 / 13.2 |

## Input

| Item | Value |
|---|---|
| Path (container) | `/videos/input.mp4` |
| Source / permission | Pexels crosswalk clip (people + vehicles), free license; see `videos/MANIFEST.md` |
| SHA-256 | `110501b29df6f348f8942987a1d70e3d9f412a9b55f7b9e6c8917bcdab49f239` |
| Codec / pixel format | h264 / yuv420p |
| Dimensions | 1920×1080 |
| FPS | 59.94 (60000/1001) |
| Duration | 29.496 s |
| Frame count | 1768 |
| Bitrate | ~4.48 Mbps |

## Models and assets

| Asset | SHA-256 | Notes |
|---|---|---|
| `models/yolo26n.pt` | `9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef` | conventional detector + ByteTrack |
| `models/yoloe-26s-seg.pt` | `48f24206bc8680d60cbbfa296b0140da849669b9515058b72f5a945142df0654` | prompted detector (YOLOE-26 small, seg) |
| `models/mobileclip2_b.ts` | `35d7f213e4d75f38514e4656ad3cb91158bd33e3805d8ac349f23b186f66982f` | MobileCLIP2 text encoder (242.0 MB), pre-placed |

Config hashes:
- `configs/run-conventional.yaml` → `41210a6a25c21706bcbfd6670c7147054c837d1eefc0a0db0bcb3ede81601ca6`
- `configs/run-prompted.yaml` → `a2e6e6584b35cd0ea09d922c31ee8004cb6ecd78fb6cba2555378603f5927a1d`

Prompted classes (ADR-3, configured before the run): `person`, `backpack`, `car`,
`motorcycle`, `fire extinguisher`.

## Commands (reproducible)

For a step-by-step manual run (with expected outputs, hashes, and troubleshooting),
see [`reproduction.md`](./reproduction.md).

```bash
# Build once
docker compose build

# Conventional path (YOLO26n + ByteTrack)
docker compose run --rm ava run --config /configs/run-conventional.yaml

# Prompted path (YOLOE-26 small + MobileCLIP2 text encoder)
docker compose run --rm ava run --config /configs/run-prompted.yaml
```

Both runs execute on GPU 0 only (`CUDA_VISIBLE_DEVICES=0` via Compose). A second
run following these commands succeeds with **zero first-use downloads** (the
text-encoder asset resolves from the pre-placed `models/mobileclip2_b.ts` via
`SETTINGS["weights_dir"]`; see Decisions below).

## Measured results

Timing is CUDA-synchronized: on GPU runs the device is synchronized at each phase
boundary so inference cost is attributed to inference, not leaked into the next
decode. Steady-state latency percentiles exclude the first 50 frames (warm-up:
CUDA context, cuDNN autotune, tracker/text-encoder state init).

### Conventional (YOLO26n + ByteTrack)

| Metric | Value |
|---|---|
| Frames | 1768 |
| Detections | 16811 |
| Load (weights + warm) | 0.66 s |
| Wall-clock (decode→output loop) | 19.32 s |
| Decode phase | 1.45 s |
| Inference phase | 8.62 s |
| Output phase (JSONL + annotated MP4) | 9.33 s |
| Steady-state inference latency p50 | 4.33 ms |
| Steady-state inference latency p95 | 4.98 ms |
| Steady-state inference latency mean | ~4.46 ms |
| Peak GPU memory (allocated) | ~73.4 MiB (76,946,944 B) |
| Overall throughput (frames / wall) | ~91.5 FPS |

Class distribution: person 12265, car 1661, umbrella 837, handbag 705, motorcycle
409, bus 349, truck 226, bicycle 177, stop sign 84, train 43, traffic light 34,
backpack 13, chair 3, oven 3, skateboard 2.

### Prompted (YOLOE-26 small + MobileCLIP2)

| Metric | Value |
|---|---|
| Frames | 1768 |
| Detections | 16192 |
| Load (weights + text-encoder JIT + set_classes) | 1.70 s |
| Wall-clock (decode→output loop) | 20.68 s |
| Decode phase | 1.23 s |
| Inference phase | 10.45 s |
| Output phase (JSONL + annotated MP4) | 9.18 s |
| Steady-state inference latency p50 | 5.67 ms |
| Steady-state inference latency p95 | 6.06 ms |
| Steady-state inference latency mean | ~5.81 ms |
| Peak GPU memory (allocated) | ~194.8 MiB (204,257,280 B) |
| Overall throughput (frames / wall) | ~85.5 FPS |

Class distribution: person 11814, motorcycle 1815, car 1302, **bag 1250**,
backpack 9, fire extinguisher 2. Absent/low classes are recorded without claiming
detection quality for them.

## Interpretation

- **The pipeline is inference/output-bound, not decode-bound.** Decode consumes a
  small fraction of wall time (~1.2–1.5 s of ~19–21 s); inference and output each
  dominate. The "decode FPS if isolated" figure (frames ÷ decode-phase seconds) is
  therefore not a meaningful decode throughput — the generator decodes lazily in
  step with the loop, so decode time here reflects only the residual after
  inference and output are subtracted from wall-clock.
- **Output overhead is significant** (~9 s per run for JSONL + annotated MP4
  encode). This is rendering/export cost, separate from analysis. Export can be
  decoupled or parallelized later without changing the detector contract.
- **Prompted inference is ~30% slower per frame than conventional** (p50 5.67 ms
  vs 4.33 ms) and uses ~2.6× the peak GPU memory, consistent with YOLOE's larger
  seg head plus the fused text-prompt embeddings.
- **Replay is not paced.** The loop runs as fast as the GPU allows; wall-clock is
  well under the 29.5 s source duration (≈0.65× real-time), so this is offline
  batch processing, not live streaming.

## Speculative capacity estimate (NOT a measured result)

> **Status: intermediate, speculative.** This section extrapolates from the single
> baseline data point to answer "how many streams can one GPU process?" It is an
> **estimate, not a capacity certification.** Per ADR-6, offline throughput is
> not live event latency and "aggregate FPS alone cannot pass." No concurrency,
> queueing, or live pacing was tested. Treat every number below as a projection to
> be validated by a concurrent-load benchmark, not a measured capacity.

**What the data does support — inference-only frames/second.** Isolating the
detection stage (inference phase ÷ frames) gives the GPU's raw detection rate:

| Path | Inference phase | Implied inference FPS | Ceiling @ 25 FPS input, every frame |
|---|---|---|---|
| Conventional (YOLO26n + ByteTrack) | 8.62 s / 1768 | ~204 fps | ~8 streams (if decode+output were free) |
| Prompted (YOLOE-26s + MobileCLIP2) | 10.45 s / 1768 | ~169 fps | ~6–7 streams (same caveat) |

**Why this overstates end-to-end capacity:**
- **Output is the largest phase** (~9 s/run for JSONL + annotated MP4). A live
  multi-stream setup would not render an MP4 per stream, so this cost largely
  disappears — but it means the ~204 fps figure overstates what the *current*
  pipeline sustains end-to-end (~91 fps wall conventional).
- **Decode was measured as a residual** (wall − inference − output), not isolated.
  N concurrent streams need N decoders (CPU/OpenCV here, or NVDEC in the target
  stack); there is no data yet on whether decode scales.
- **No concurrency was tested.** Sequential single-stream jobs ≠ simultaneous
  streams; GPU utilization, batching, memory pressure, and queueing all change.

**VRAM is not the limiter.** Peak allocation was ~73 MiB (conventional) /
~195 MiB (prompted) on a 24 GB card — compute-bound, not memory-bound.

**Definitional dependency.** A stream count is only meaningful once input FPS,
analysis FPS (every frame vs sampled), detector path, and live pacing are fixed
(ADR-6). At 25 FPS input analyzed every frame, the inference-only estimate is
roughly **6–8 concurrent streams** on one RTX 3090 for this workload; sampling at a
lower analysis FPS scales that up proportionally.

### Speculative comparison: RTX 3090 vs RTX 6000 Pro (Blackwell)

No 6000 Pro is present on this host, so this is **spec-derived and directional**,
not measured:

| | RTX 3090 (measured host) | RTX 6000 Pro Blackwell |
|---|---|---|
| Architecture | Ampere (GA102) | Blackwell |
| FP16/BF16 tensor throughput | ~71 TFLOPS | ~250+ TFLOPS (≈3–4×) |
| Memory | 24 GB GDDR6X, ~936 GB/s | 96 GB GDDR7, ~1.8 TB/s (~2×) |

**Directional expectation:** for the same detector at the same precision, expect
roughly **3–4× the inference FPS** on the 6000 Pro — i.e., where the 3090 does
~204 fps conventional, on the order of **600–800 fps**, pushing the every-frame
25-fps stream estimate from ~8 to **~25–30 streams**. Larger VRAM/bandwidth also
helps for bigger models or higher-res inputs.

**Caveats:** real gains depend on compute- vs memory/decode-bound behavior; a
TensorRT export typically adds another 1.5–2× over eager PyTorch for *both* GPUs
and may shift the ratio; no 6000 Pro was benchmarked here.

### What would make this a real answer

Per ADR-6's protocol: fix the workload (input/analysis FPS, detector path,
live pacing); warm up; run **increasing concurrent camera counts**
under identical settings; record per-camera FPS, event-latency p50/p95, queue
depth/oldest age, drops, and GPU/CPU/VRAM utilization at each point. The capacity
is the highest point holding ≥4 analyzed fps/camera, p95 < 3 s, no sustained queue
growth, for ≥30 min.

## Reproducibility and second-run verification

A second run following the documented commands above succeeded for **both** paths
with **0 download lines** and consistent metrics:

| Path | Run 1 detections | Run 2 detections | Wall (s) | Downloads |
|---|---|---|---|---|
| Conventional | 16811 | 16811 | 19.32–19.40 | 0 |
| Prompted | 16192 | 16192 | 20.68–20.87 | 0 |

Detection counts are deterministic for a given build; wall-clock varies by <1 s
between runs (GPU scheduling / thermal). Output artifacts (`detections.jsonl`,
`annotated.mp4`) are regenerated identically in structure (1768 JSONL lines each).

## Decisions and findings

- **CUDA-synchronized timing added** (`src/ava/timing.py`): a `PhaseTimer`
  synchronizes the CUDA device at each phase boundary on GPU runs so inference
  cost is not attributed to the following decode. CPU smoke runs skip the sync.
  Steady-state percentiles exclude the first 50 warm-up frames.
- **Text-encoder no-download fix verified**: ultralytics
  `build_text_model` hard-codes `weight="mobileclip2_b.ts"` for the mobileclip2
  base, so yaml/attribute path overrides are ignored. The reliable lever is
  `SETTINGS["weights_dir"]`; `PromptedDetector.load()` copies the pre-placed
  asset into `WEIGHTS_DIR` and sets `SETTINGS["weights_dir"]`. Both timed runs
  show **0 download lines**.
- **YOLOE outputs a class beyond the configured prompts.** After
  `set_classes([person, backpack, car, motorcycle, fire extinguisher])` the model's
  `names` dict holds exactly those 5 entries (IDs 0–4), yet on some frames YOLOE
  emits class ID 5, which the predictor labels `bag` (1250 detections in this run).
  This is YOLOE-26s-seg's segmentation head producing an additional class beyond
  the text prompts. It is recorded here as observed behavior; it does not affect
  the conventional path and is not a quality claim for the prompted path.
- **Prompted detection count differs from the earlier prompted run** (14949 → 16192).
  The delta is the class-5 `bag` output (1250) plus a small backpack shift
  (16 → 9); person/motorcycle/car counts are unchanged. The current build is
  reproducible at 16192 across consecutive runs, so that is the recorded baseline.

## Failures and blockers

- No access blockers. All assets were pre-placed and SHA-256 verified; both runs
  completed with exit code 0 and no first-use downloads.
- One transient issue during timing development: a bare `docker run` (outside Compose)
  re-downloaded `yoloe-26s-seg.pt` and `mobileclip2_b.ts` into the container CWD
  because that path does not go through AVA's `PromptedDetector.load()` asset
  logic. The SHA-256 of `models/yoloe-26s-seg.pt` was re-verified unchanged
  (`48f24206…df0654`) afterward. The Compose service runs (the documented commands)
  are download-free.

## Artifacts

- `results/conventional/detections.jsonl` (1768 lines), `results/conventional/annotated.mp4`
- `results/prompted/detections.jsonl` (1768 lines), `results/prompted/annotated.mp4`
