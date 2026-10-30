# Adaptive Video Analytics (AVA)

Technology overview and versioning specification: [STACK.md](STACK.md).

AVA is an independent, free/self-hosted video-analytics learning project. Its
mission is to build useful low-cost analytics, measure real capability and
throughput on homelab hardware, and use the work to strengthen practical video
analytics, IT, software-development and architecture skills. The repository is
also a proving ground for disciplined, human-gated agent-driven development.

The implemented study paths include a single-camera offline proof of concept
and a provisional multi-camera RTSP runner. Both support a **conventional**
detector (YOLO26n + ByteTrack) or a **text-prompted** detector (YOLOE-26 small),
plus line-crossing, zone-entry and dwell rules. The live runner separates decode
from analysis with bounded per-camera queues and source-time pacing.

The E-3 capacity result remains provisional: the original sweep completed, but
its pacing and queue instrumentation prevented a valid stable-capacity claim.
DeepStream compatibility, the local observability stack and replay harness have
measured evidence; databases and the product API/UI remain later work. See
`docs/` (mostly private, not in Git) for planning and progress; the stable
architecture ADRs and test rules are committed under `docs/architecture/` and
`docs/tests_rules.md`.

The batching foundation provides correlated detection/tracker interfaces and a
CPU-tested fair scheduler with bounded candidates, in-flight work and shutdown.
Live activation and the real single-model executor remain pending. The
`run-e3-*-conv-batched.yaml` examples are pending configurations and are rejected
by the current live configuration validator.

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
benchmarks/e-3/   DeepStream evidence and provisional live-capacity measurements
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

## PostgreSQL persistence

**Breaking change (B-023):** PostgreSQL is the mandatory default backend.
Event-producing camera and live runs require a reachable PostgreSQL server
unless the config explicitly sets `persistence.backend: jsonl`. Detection-only
file runs do not connect to an event sink.

To use PostgreSQL, set `AVA_PG_DSN` in the environment (or provide
`persistence.dsn` in the config) and apply the schema first:

```bash
AVA_PG_DSN=postgresql://ava:***@localhost:5432/ava alembic upgrade head
```

The inference image includes the migration files; run Alembic with the
image entrypoint overridden. PostgreSQL must be reachable at startup;
if persistence fails during a live run, all cameras stop and the run
fails.

**Container-to-database path:** Start the ops stack first, then attach the
worker to its Docker network with the PostgreSQL Compose override. The host
port remains bound to loopback. Use `postgres` as the database hostname inside
the worker; use `localhost` for migrations run on the host. Copy an event-producing
config under `configs/` and change its persistence backend to `postgres`:

```bash
AVA_PG_DSN='postgresql://ava:YOUR_PASSWORD@postgres:5432/ava' \
  docker compose -f docker-compose.yml -f docker-compose.postgres.yml \
  run --rm ava run --config /configs/your-postgres-config.yaml
```

For file camera runs, set an optional `run_id` in the configuration to reuse
event identities when retrying the same input. Omit it for a new run; AVA
generates an identity, visible as the prefix before `-s1` in the first event's
`session_id`. Reusing an ID for different input can cause distinct events to be
treated as duplicates. A run with no events does not record the generated ID;
specify one beforehand if it must be recoverable for a retry.

To opt out (file-only mode), add to the run config:

```yaml
persistence:
  backend: jsonl
```

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
`docs/tests_rules.md`.

## API authentication and readiness

Construct the API app with an `IdentityPolicy` to enable authenticated access
to version and capabilities. Protected routes reject requests if no policy is
configured. Liveness and readiness remain open. The canonical OpenAPI contract is `src/ava/api/openapi.yaml`; it is included
in the package and served by the application.
The separate local Keycloak and identity database stack is documented in
[ops/README.md](ops/README.md#local-identity-provider).
Readiness contributors may set `required = False` for optional dependencies;
contributors default to required when the attribute is absent.
