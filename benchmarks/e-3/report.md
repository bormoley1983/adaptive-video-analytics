# E-3-T1: DeepStream 9.2 compatibility spike

Status: **partial compatibility passed; E-3-T1 remains in progress**.

## Verdict

Continue the compatibility spike. DeepStream 9.2 starts on the RTX 3090 and a
stock NVIDIA ResNet18 path exercised GPU decode and TensorRT inference, but this
does not yet establish compatibility for AVA's models or satisfy the E-3-T1
gate. Do not start capacity claims from this result.

The remaining gate requires a YOLO26 export with the correct DeepStream parser
and postprocessing, a detection comparison against the E-2 baseline, and a
separate YOLOE export/class-change test.

## Reproducibility manifest

| Component | Measured value |
|---|---|
| Host | `ai-host1`, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| GPU | RTX 3090, device 0, UUID `GPU-facd1ddc-c4ab-7ac9-0000-ead909e0ec42` |
| Driver | 595.99.02 |
| DeepStream | 9.2 |
| Container | `ghcr.io/nvidia/deepstream:9.2-samples-multiarch` |
| Image digest | `sha256:5ee7458d5e795592e0cb182457a7f8281f2c1da7bb38ce4b8506ac693f266746` |
| GStreamer | 1.24.2 |
| TensorRT | 10.16.0.72 |
| CUDA / cuDNN | 13.2 / 9.20.0.48 |
| Input | H.264 High, 1280x720, 30 FPS, 48.066 s, 2,455,066 bit/s stream bitrate |
| Input SHA-256 | `a9a62dc35aedb8367ca76549334e66ae1980a03a9dee17ff39ca4bbd981341bf` |
| Input storage | Local ignored file `benchmarks/e-3/t1-sample-720p-vonly.mp4`; not distributed in Git |

The container digest and media metadata were rechecked locally on 2026-10-22.
The source clip itself remains outside Git under the repository media policy.

## What this spike validates

### Container and GPU visibility

The pinned DeepStream 9.2 image launched with GPU 0 and reported the expected
RTX 3090. NVIDIA documents DeepStream 9.2 as supporting Ampere GPUs and Ubuntu
24.04 on x86; this host run supplies the project-specific compatibility check.

```bash
docker run --rm --gpus '"device=0"' \
  ghcr.io/nvidia/deepstream@sha256:5ee7458d5e795592e0cb182457a7f8281f2c1da7bb38ce4b8506ac693f266746 \
  nvidia-smi -L
```

### Stock-model decode and inference path

- A stock NVIDIA ResNet18 ONNX model produced an FP16 TensorRT engine in 19.6
  seconds.
- `nvv4l2decoder`, `nvvideoconvert` and `nvinfer` ran in a manually constructed
  one-stream GStreamer pipeline through end of stream.
- The run is functional compatibility evidence only. It did not archive
  per-frame detections, quantify drops or stalls, measure latency/throughput,
  or exercise AVA's detector, tracker, rules or event contracts.

The manual pipeline used the NVIDIA stock primary-inference configuration:

```text
filesrc -> qtdemux -> h264parse -> nvv4l2decoder -> nvvideoconvert
        -> nvinfer -> fakesink
```

## Inconclusive `deepstream-app` attempt

The attempted `deepstream-app` file-source run reported 0 FPS and a data-flow
before segment-event warning. The committed diagnostic configuration also set
`model-engine-file` to an `.onnx` path. NVIDIA defines `model-engine-file` as a
serialized TensorRT engine path; an ONNX input belongs in `onnx-file` within the
`nvinfer` configuration.

Because the attempted configuration violated that contract, the 0 FPS result
does **not** establish a DeepStream 9.2 sample bug. Preserve
`t1-headless-one-stream.txt` as the exact failed-attempt configuration and
repeat with a valid engine/config before assigning a root cause.

## Not yet validated

- YOLO26 ONNX/TensorRT export, parser and postprocessing;
- detection parity with the E-2 baseline (16,529 detections / 267 tracks);
- YOLOE export and runtime class-change behavior;
- RTSP ingest, reconnection or `nvmultiurisrcbin` on this host;
- input, decoded and analyzed FPS; drops; queue behavior; latency; or resource
  utilization;
- tracker/rule/event parity or any multi-stream capacity point.

NVIDIA documents `nvurisrcbin` and `nvmultiurisrcbin`, but component availability
is not evidence that this project's RTSP workload works. E-3-T2 remains blocked
on a valid T1 model path and a separately measured RTSP harness.

## Correct DeepStream model properties

| Input | Property |
|---|---|
| ONNX model to build | `onnx-file=/path/model.onnx` in the `nvinfer` config |
| Existing TensorRT engine | `model-engine-file=/path/model.engine` |
| Gst-nvinfer configuration | `config-file-path=/path/config_infer.txt` |

References checked 2026-10-22: NVIDIA DeepStream 9.2
[overview](https://docs.nvidia.com/deepstream/text/DS_Overview),
[release notes](https://docs.nvidia.com/deepstream/text/DS_Release_notes),
[custom-model guide](https://docs.nvidia.com/deepstream/text/DS_using_custom_model),
[`nvinfer` properties](https://docs.nvidia.com/deepstream/text/DS_plugin_gst-nvinfer),
and [`nvmultiurisrcbin`](https://docs.nvidia.com/deepstream/text/DS_plugin_gst-nvmultiurisrcbin).

## Exact next actions for E-3-T1

1. Correct the stock `deepstream-app` configuration and archive the command,
   exit status and frame/performance output so the file-source result is
   diagnosable.
2. Export the E-2 conventional model, configure the required parser and
   postprocessing, then compare detections on the checksummed input with fixed
   confidence, IoU, precision and image-size settings.
3. Export and test the prompted model separately, including a class-profile
   change without rebuilding unrelated pipeline state.
4. Record model/config hashes, warm-up, synchronized timings, GPU/CPU/RAM/VRAM
   use and failure evidence. Only then issue the T1 go/no-go decision.

No E-3 capacity point, production readiness or migration approval is claimed by
this partial result.
