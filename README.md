# Adaptive Video Analytics (AVA)

Single-camera video detection proof of concept. Runs a **conventional** detector
(YOLO26n + ByteTrack) and a **text-prompted** detector (YOLOE-26 small)
sequentially on one MP4, producing an annotated video and per-frame JSONL
detection records with honest speed measurements.

This is the E-1 slice: no RTSP, DeepStream, services, databases or dashboards.
See `docs/` (private, not in Git) for planning, ADRs and progress.

## Layout

```
src/ava/          Python package: config, ingest, detector, output, runner, cli
configs/          Sanitized YAML run configs (conventional + prompted)
scripts/          Asset fetch helper
tests/            Deterministic contract checks (no GPU required)
benchmarks/e-1/   Baseline report and measured artifacts
Dockerfile        CUDA inference image
docker-compose.yml  GPU-enabled one-shot run
```

## Prerequisites

- Docker Engine + Compose with the NVIDIA Container Toolkit.
- One people/vehicle MP4 placed at `videos/input.mp4` (read-only mount).
- Model assets in `models/` (run `scripts/fetch-assets.sh`, or let the image
  warm them on first use).

## Run

```bash
# Conventional path
docker compose run --rm ava run --config /configs/run-conventional.yaml

# Prompted path (same clip, same GPU)
docker compose run --rm ava run --config /configs/run-prompted.yaml
```

Outputs land under `results/<path>/`: `detections.jsonl` and `annotated.mp4`.
Override the GPU with `AVA_GPU=<index-or-uuid> docker compose run ...`.

## Tests (no GPU)

```bash
python -m pytest tests/ -q
ruff check src tests
```

These validate config parsing and the JSONL contract only; they do not certify
GPU inference.
