# Adaptive Video Analytics (AVA)

AVA is an independent, free/self-hosted video-analytics learning project. Its
mission is to build useful low-cost analytics, measure real capability and
throughput on homelab hardware, and use the work to strengthen practical video
analytics, IT, software-development and architecture skills. The repository is
also a proving ground for disciplined, human-gated agent-driven development.

The currently implemented slice is a single-camera offline proof of concept. It runs a
**conventional** detector (YOLO26n + ByteTrack) or a **text-prompted** detector
(YOLOE-26 small) on MP4 input. Configuration can add line-crossing, zone-entry
and dwell rules, producing annotated video plus per-frame detection and event
JSONL with measured timing.

Implemented scope: single-camera offline PoC — no RTSP, DeepStream, services,
databases or dashboards yet. The long-term roadmap retains an independent AVA
API and UI after the runtime is measured and reliable. See `docs/` (private,
not in Git) for planning, ADRs and progress.

The reference homelab has two independent 24 GB RTX 3090 GPUs. One normally
serves the local-AI stack and one is shared by AVA and other projects. AVA must
measure live allocation before a run. The GPUs may be configured as a temporary
test pool only for an explicitly approved experiment; they are never treated as
pooled VRAM or permanently dedicated capacity.

## Layout

```
src/ava/          Ingest, detector/tracker, rules, events, output, runner, CLI
configs/          Sanitized detector and E-2 rule-run YAML examples
scripts/          Asset fetch helper
tests/            Deterministic contract checks (no GPU required)
benchmarks/e-1/   Measured detector baseline
benchmarks/e-2/   Measured rule demonstration and current limitations
benchmarks/e-3/   DeepStream compatibility evidence; capacity remains pending
requirements.lock.txt  Exact tested runtime dependency closure used by Docker
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

# Conventional detection/tracking plus configured rules
docker compose run --rm ava run --config /configs/run-e2-rules.yaml

# Empty-geometry negative rule run
docker compose run --rm ava run --config /configs/run-e2-negative.yaml
```

Outputs land under `results/<path>/`: `detections.jsonl` and `annotated.mp4`.
Rule-enabled runs also write `events.jsonl`. The event/evidence contract,
snapshot/clip materialization and run isolation exist, but evidence-window
correctness remains subject to the documented validation gates; do not treat a
locator as verified until its file resolves.
Override the GPU with `AVA_GPU=<index-or-uuid> docker compose run ...`.

## Development checks

```bash
python3 -m pip install -e '.[dev]'
make test-unit
make test-integration
make test-e2e
make test-infra
make test-coverage
make lint
```

The suites are intentionally separate because they prove different boundaries.
Unit tests require no GPU. End-to-end and infrastructure tests may require the
declared media, models, container runtime, GPU or services. An empty selected
suite exits as not collected rather than being treated as a pass. The coverage
gate requires at least 90% combined line and branch coverage; meaningful tests
of changed behavior remain more important than maximizing the percentage. See
`docs/tests_rules.md` when the private documentation tree is available.
