# Gate 4: Device Isolation Policy + Compatibility Go/No-Go

## Device Isolation Policy

### Observed behavior

| Exposure | NVDEC result | Inference result |
|----------|-------------|-----------------|
| `--gpus device=0` (single GPU) | `CUDA_ERROR_NO_DEVICE` (100) | Not reached |
| `--gpus all` + `CUDA_VISIBLE_DEVICES=<UUID>` | `CUDA_SUCCESS` (0) | Works |

The NVDEC hardware decoder requires broader device-node visibility than a
single-GPU Docker exposure provides, even when CUDA is pinned to one GPU via
UUID. The internal driver mechanism is not publicly documented; NVIDIA forum
reports describe similar per-device-exposure behavior without root-cause
explanation.

### Policy recommendation

**For benchmarking and development (current phase):**

Use `--gpus all` with `CUDA_VISIBLE_DEVICES` pinned to the target GPU UUID.
This is the owner-approved experimental workaround. It provides:
- Correct NVDEC hardware decode
- CUDA compute pinned to the intended GPU
- No interference with other GPUs' workloads (verified: llama-server on GPU 1
  unaffected)

**For production deployment (future phase):**

The permanent isolation policy must be resolved before E-4/E-5 capacity work.
Options, in order of preference:

1. **Docker `--gpus device=<UUID>` with driver update.** If a future NVIDIA
   driver (≥ 595.x) fixes the NVDEC single-device exposure issue, revert to
   per-GPU Docker isolation. This is the cleanest boundary. Re-test on each
   driver upgrade.

2. **cgroup v2 GPU partitioning (MIG or time-slicing).** If the host runs a
   driver supporting MIG (not available on RTX 3090) or if NVIDIA's
   `nvidia-container-toolkit` gains per-device NVDEC isolation, use that.
   Not currently viable on this hardware.

3. **Dedicated GPU per worker with `--gpus all` + UUID pinning.** Accept the
   broader device-node visibility as a deployment constraint. Mitigate by:
   - Running each AVA worker in its own container with `CUDA_VISIBLE_DEVICES`
     pinned to its assigned GPU UUID
   - Documenting that the container can *see* other GPUs but cannot *use*
     them (CUDA runtime enforces the pin)
   - Monitoring for accidental cross-GPU allocation via `nvidia-smi`
   - Recording this as an accepted risk in the operations runbook

4. **Bare-metal / VM isolation.** If container-level GPU isolation proves
   insufficient, allocate a dedicated VM or bare-metal partition per GPU.
   Highest cost; last resort.

**Decision for E-3:** Option 3 is the working policy for all remaining E-3
benchmarking. The broader visibility is an accepted experimental risk with
CUDA UUID pinning as the enforcement mechanism. This is NOT a production
approval; it must be revisited before any deployment decision.

### Verification performed

- Post-run `nvidia-smi` confirmed only pre-existing workloads (ComfyUI on GPU 0,
  llama-server on GPU 1) — no cross-GPU allocation by the AVA container.
- CUDA_VISIBLE_DEVICES pinning verified via `probe_nvdec.py`: exactly 1 visible
  device, correct UUID.
- No driver, shared service or permanent GPU configuration was changed.

## Full Compatibility Go/No-Go Verdict

### Gate summary

| Gate | Criterion | Result |
|------|-----------|--------|
| 1 | Coordinate-space fix + IoU accuracy vs E-2 | **PASS** (directional: +1.93% count; box-level IoU matching performed; no labelled ground truth for absolute accuracy) |
| 2 | Throughput/timing measurement | **PASS** (415.8 FPS wall, 5.2× faster than E-2 Python baseline; ~2.41 ms/frame avg) |
| 3 | YOLOE export + class-change behavior | **PASS** (ONNX→TRT works; parser validated; distinct model per prompt set; runtime mutation rejected per ADR-3) |
| 4 | Device isolation policy + go/no-go | **This document** |

### Verdict: **GO — with conditions**

DeepStream 9.2 on the RTX 3090 reference host is **compatible** for the AVA
video analytics workload, subject to the following conditions and caveats:

#### Confirmed working

- NVDEC hardware decode (H.264, 1080p) with `--gpus all` + UUID pinning
- TensorRT FP16 inference (YOLO26n conventional, YOLOE-26s-seg prompted)
- Custom C++ parser integration (both model families)
- Full pipeline: decode → mux → nvinfer → custom parse → JSONL output
- 5.2× throughput improvement over the E-2 Python/Ultralytics baseline
- Static model export and profile-based class management (ADR-3 compliant)

#### Conditions / caveats

1. **Device isolation is experimental.** The `--gpus all` workaround must be
   replaced with a proper isolation mechanism before production deployment.
   See policy options above. This does NOT block further E-3 benchmarking.

2. **No absolute accuracy certification.** The +1.93% detection count delta vs
   E-2 is directional only. No labelled ground-truth dataset exists for this
   clip. Per-box IoU matching was performed (Gate 1) but without a reference
   annotation set, absolute mAP cannot be claimed. Quality approval requires
   either a labelled subset or owner sign-off on the directional comparison.

3. **Single-stream only.** This validates one stream at batch-size=1. Multi-
   stream capacity (T5), tracker integration, rule parity under DeepStream and
   30-minute stability are NOT established by this spike.

4. **YOLOE segmentation masks unused.** The T1 parser decodes only the
   detection head (`output0`). Segmentation mask output (`output1`) is not
   parsed; if AVA requires instance masks, a separate parser extension is
   needed.

5. **No tracker in this run.** NvDCF or other tracking was disabled. The E-2
   baseline included 267 tracks; DeepStream track parity is untested.

6. **GPU shared with other workloads.** ComfyUI runs on GPU 0 concurrently.
   Timing measurements are not isolated; production capacity claims require
   a dedicated benchmark window.

#### What this does NOT approve

- Production deployment of the DeepStream pipeline
- Multi-stream capacity claims (T5)
- Quality/accuracy certification for operational use
- The `--gpus all` exposure as a permanent configuration
- Migration from the Python baseline (E-4 gate requires T5 evidence)

#### Next steps to close E-3-T1

- [ ] Owner sign-off on directional accuracy comparison (or provide labelled
      ground truth for absolute mAP)
- [ ] Record this go/no-go in `docs/progress/` and update ADR-2 status from
      "Proposed pending E-3" to reflect the compatibility result
- [ ] Proceed to T5 capacity sweep with the device-isolation policy above

## Reproducibility

All commands, container digests, engine/parser SHA-256 hashes and run logs are
recorded in `report.md` (reproducibility manifest) and `gate3-yoloe-findings.md`.
The detection output evidence is `t1-yoloe-detections.jsonl` (1768 frames) and
the YOLO26n output referenced in the report.
