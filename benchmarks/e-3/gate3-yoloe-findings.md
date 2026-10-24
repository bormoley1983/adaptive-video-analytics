# Gate 3: YOLOE Export + Class-Change Behavior

## Summary

YOLOE-26s-seg exports to ONNX and TensorRT successfully. A custom DeepStream
parser correctly decodes the detection head output. Different prompt sets
produce structurally different models (different channel counts), confirming
the ADR-3 immutability contract: classes are baked in at export time and
cannot be changed at runtime.

## Export

| Artifact | Size | Notes |
|----------|------|-------|
| `yoloe-26s-seg.onnx` | 39.8 MB | Dual output: det `[1,41,8400]` + masks `[1,32,160,160]` |
| `yoloe-26s-seg-det-only.onnx` | ~35 MB | Single output: det `[1,41,8400]` only |
| `yoloe-26s-seg.engine` | 24 MB | TRT FP16, dual output |
| `yoloe-26s-seg-det-only.engine` | 22.6 MB | TRT FP16, single output (used in pipeline) |

Export command (Ultralytics 8.4.183, torch 2.14.1+cu130):
```bash
m = YOLOE('yoloe-26s-seg.pt')
m.set_classes(['person','backpack','car','motorcycle','fire extinguisher'])
m.export(format='onnx', imgsz=640, half=False, dynamic=False, device=0)
```

TRT build (static model — no shape flags):
```bash
trtexec --onnx=yoloe-26s-seg-det-only.onnx --saveEngine=yoloe-26s-seg-det-only.engine --fp16
```

## Detection Head Layout

Width = `4 + N_prompts + 32` (box coords + per-prompt class scores + prototype weights)

| Prompt set | Channels | Output shape |
|------------|----------|--------------|
| 5 prompts (person, backpack, car, motorcycle, fire extinguisher) | 41 | `[1, 41, 8400]` |
| 3 prompts (person, bus, truck) | 39 | `[1, 39, 8400]` |

## Class-Change Behavior

1. **Different prompt sets → different models.** Exporting with 3 prompts
   instead of 5 changes the output tensor width from 41 to 39 channels.
   The models are structurally incompatible; a parser written for one
   cannot parse the other without recompilation.

2. **Runtime mutation rejected.** Calling `model.set_classes(...)` on an
   already-exported ONNX model raises `AssertionError`. This confirms the
   ADR-3 contract: "YOLOE static exports bake in classes and reject runtime
   class/visual-prompt changes."

3. **Profile identity.** Each prompt set produces a distinct engine file.
   Versioning is by filename (e.g., `yoloe-26s-seg-det-only.engine` for the
   5-prompt set). Rollback = restore the previous engine file. No shared
   state between profiles.

## DeepStream Pipeline Validation

**Result: PASS.** Custom parser `NvDsInferParseCustomYoloE` correctly
decodes the `[1,41,8400]` detection tensor in a full DeepStream 9.2 pipeline.

- **Video:** crosswalk-day-pedestrians.mp4 (1768 frames, 1920×1080)
- **Detections:** 1768 JSONL records written (one per frame)
- **Classes observed:** person (id 0), car (id 2), motorcycle (id 3)
- **Confidence range:** 0.38–0.90 (person highest, as expected on crosswalk)
- **Coordinate space:** Frame pixels (letterbox inverse transform applied)
- **Engine latency (trtexec):** ~1.69 ms GPU, 591 qps

### Key Configuration Findings

| Parameter | Working value | Note |
|-----------|--------------|------|
| `config-file=` in `[primary-gie]` | Required key name | `infer-config-file=` does NOT work in DS 9.2 |
| `model-engine-file=` | In nvinfer config | Not `engine-file=` |
| `network-mode=2` | FP16 | Must match engine precision |
| `cluster-mode=4` | None (no clustering) | Parser does its own NMS |
| `custom-lib-path` | Absolute path to .so | Library must be loadable in container |
| Video file | Must exist at URI path | Missing file → "Resource not found", no parser call |

### Root Cause of Initial Failure

The custom parser was never invoked because the video file
(`/tmp/crosswalk.mp4`) was absent from the container. Without a valid source,
nvinfer never received inference buffers and the parse function was never
called. No error was logged for the missing custom library — the pipeline
simply produced no detections.

## Artifacts

| File | Purpose |
|------|---------|
| `nvdsinfer_custom_yoloe.cpp` | Parser source (clean, no debug) |
| `libnvdsinfer_custom_yoloe.so` | Compiled parser (23 KB) |
| `config_infer_primary_yoloe.txt` | nvinfer config |
| `t1-yoloe-one-stream.txt` | Pipeline config |
| `run_yoloe_one_stream.sh` | Reproducible run script |
| `t1-yoloe-detections.jsonl` | 1768-frame detection output (evidence) |

## Verdict

Gate 3 acceptance criteria met:
- [x] YOLOE exports to ONNX and TRT engine
- [x] Custom parser correctly decodes detection output in DeepStream
- [x] Class-change behavior tested: distinct model per prompt set, runtime mutation rejected
- [x] Profile identity/versioning/rollback documented (filename-based)
