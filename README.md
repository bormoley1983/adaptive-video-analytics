# Adaptive Video Analytics (AVA)

Technology overview and versioning specification: [STACK.md](STACK.md).

AVA is an independent, free/self-hosted video-analytics learning project. Its mission is to build useful low-cost analytics, measure real capability and throughput on homelab hardware, and use the work to strengthen practical video analytics, IT, software-development and architecture skills. The repository is also a proving ground for disciplined, human-gated agent-driven development.

The implemented study paths include a single-camera offline proof of concept and a provisional multi-camera RTSP runner. Both support a **conventional** detector (YOLO26n + ByteTrack) or a **text-prompted** detector (YOLOE-26 small), plus line-crossing, zone-entry and dwell rules. The live runner separates decode from analysis with bounded per-camera queues and source-time pacing.

The live-capacity result remains provisional: the original sweep completed, but its pacing and queue instrumentation prevented a valid stable-capacity claim. DeepStream compatibility, the local observability stack and replay harness have measured evidence; PostgreSQL persistence and API behavior are described below; the product UI remains later work. Planning and progress under `docs/` remain mostly private. Public exceptions are the stable architecture ADRs, test rules, [API foundation](docs/api-foundation.md) and [API acceptance evidence](docs/api-acceptance.md), as recorded in [ADR-10](docs/architecture/adr/ADR-10.md). The API foundation is implemented; acceptance remains incomplete until container-to-provider routing is proven.

The batched live mode uses one detection model with a bounded fair scheduler and independent camera trackers/rules. Python/Ultralytics is implemented; selecting the DeepStream adapter reports that it is unavailable. The diagnostic YAML examples explicitly use JSONL; PostgreSQL remains the production default. GPU batching uses explicit IEEE FP32 arithmetic. Fresh numerical parity and four-camera smoke pass; the 50-camera matrix misses FPS/drop targets. See [the runtime checkpoint](benchmarks/e-3/python-batched-runtime.md).

The reference homelab has two independent 24 GB RTX 3090 GPUs. One normally serves the local-AI stack and one is shared by AVA and other projects. AVA must measure live allocation before a run. The GPUs may be configured as a temporary test pool only for an explicitly approved experiment; they are never treated as pooled VRAM or permanently dedicated capacity.

## Layout

```
src/ava/          Ingest, detector/tracker, rules, events, output, runner, CLI
configs/          Sanitized detector and rule-run YAML examples
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
- Model assets in `models/` (run `scripts/fetch-assets.sh`, or let the image warm them on first use).

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

Outputs land under `results/<path>/`: `detections.jsonl` and `annotated.mp4`. Rule-enabled runs also write `events.jsonl`. The event/evidence contract, snapshot/clip materialization and run isolation exist, but evidence-window correctness remains subject to the documented validation gates; do not treat a locator as verified until its file resolves. Override the GPU with `AVA_GPU=<index-or-uuid> docker compose run ...`.

## PostgreSQL persistence

**Breaking change:** PostgreSQL is the mandatory default backend. Event-producing camera and live runs require a reachable PostgreSQL server unless the config explicitly sets `persistence.backend: jsonl`. Detection-only file runs do not connect to an event sink.

To use PostgreSQL, set `AVA_PG_DSN` in the environment (or provide `persistence.dsn` in the config) and apply the schema first:

```bash
AVA_PG_DSN=postgresql://ava:***@localhost:5432/ava alembic upgrade head
```

The inference image includes the migration files; run Alembic with the image entrypoint overridden. PostgreSQL must be reachable at startup; if persistence fails during a live run, all cameras stop and the run fails.

**Container-to-database path:** Start the ops stack first, then attach the worker to its Docker network with the PostgreSQL Compose override. The host port remains bound to loopback. Use `postgres` as the database hostname inside the worker; use `localhost` for migrations run on the host. Copy an event-producing config under `configs/` and change its persistence backend to `postgres`:

```bash
AVA_PG_DSN='postgresql://ava:YOUR_PASSWORD@postgres:5432/ava' \
  docker compose -f docker-compose.yml -f docker-compose.postgres.yml \
  run --rm ava run --config /configs/your-postgres-config.yaml
```

For file camera runs, set an optional `run_id` in the configuration to reuse event identities when retrying the same input. Omit it for a new run; AVA generates an identity, visible as the prefix before `-s1` in the first event's `session_id`. Reusing an ID for different input can cause distinct events to be treated as duplicates. A run with no events does not record the generated ID; specify one beforehand if it must be recoverable for a retry.

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

The suites are intentionally separate because they prove different boundaries. Unit tests require no GPU. End-to-end and infrastructure tests may require the declared media, models, container runtime, GPU or services. An empty selected suite exits as not collected rather than being treated as a pass. The coverage gate requires at least 90% combined line and branch coverage; meaningful tests of changed behavior remain more important than maximizing the percentage. See `docs/tests_rules.md`.

## API authentication and readiness

Construct the API app with an `IdentityPolicy` to enable authenticated access to version and capabilities. Protected routes reject requests if no policy is configured. Liveness and readiness remain open. Every response carries an `X-Request-Id` header: a client-supplied canonical RFC 4122 UUID (lowercase) is preserved, otherwise AVA generates one; a malformed value yields a 400 problem response with a newly generated ID and is never reflected or logged. Error responses include the request ID in both the header and the problem body. The canonical OpenAPI contract is `src/ava/api/openapi.yaml`; it is included in the package and served by the application. The separate local Keycloak and identity database stack is documented in [ops/README.md](ops/README.md#local-identity-provider). Readiness contributors may set `required = False` for optional dependencies; contributors default to required when the attribute is absent.

## Event and camera reads (grant-scoped)

When the app is constructed with event, grant and camera repositories, it enables cursor-paginated event reads, camera inventory, and admin inspection of camera state and grants. Admin grant PUT, grant DELETE and camera DELETE require a conforming mutation transaction factory. Access is grant-based: a caller sees a camera's history only while a current trusted grant covers their tenant and passes their site restriction; revocation removes access on the next read. Events expose evidence kind without internal paths or source URLs. Missing or inaccessible events return 404 without leaking existence.

The event API requires event, grant and camera repositories together. Existing callers that construct `AppConfig` with only event and grant repositories must also provide a camera repository. For PostgreSQL, `compose_postgres_app(dsn, identity_policy=...)` constructs and validates all three repositories and closes their pools when the app shuts down. Apply Alembic migration 0004 before starting the updated service. Camera deletion hides the camera from inventory; its event history remains accessible according to current grants and site restrictions. Grant replacement creates or reactivates the camera atomically.

Bearer tokens require a matching non-empty audience and a finite numeric expiry. Missing or malformed audience/expiry claims return 401; expired numeric expiry values remain rejected as expired.

## Request identification and mutation retries

All non-probe API requests require a client-generated canonical UUID `Idempotency-Key`; health probes may omit it but validate supplied keys. Reads use it for identification only. Optional `X-Correlation-Id` is generated when absent and returned in responses; malformed values return 400. Both headers accept uppercase or lowercase hex and normalize internally. `X-Request-Id` identifies each HTTP attempt.

Grant PUT and both DELETE operations atomically store successful results with their mutations for one hour. Retry the same operation with the same key and unchanged request, including `If-Match`; different requests reusing the key return 409. After expiry an operation may execute again. Authentication and current authorization apply to every replay. Applications without a conforming mutation transaction factory reject writes rather than bypassing these guarantees; `compose_postgres_app` supplies the PostgreSQL factory.

Admin GETs for `/api/v1/grants/{camera_id}` and `/api/v1/cameras/{camera_id}` return distinct opaque ETags. Supply the target resource's tag in optional `If-Match` to prevent stale updates; tag lists and `*` are supported. Grant PUT returns its updated ETag. Successful DELETE responses return 204 without ETags; an absent grant and an already-soft-deleted camera are successful no-ops, while an unknown camera returns 404. Camera deletion preserves grants and historical access.

Mutation transactions use a separate async pool (default maximum four connections) with immediate bounded admission and five-second lock timeouts; contention or exhausted admission returns 409. Request logs contain identifiers, method, route template, status and duration, with no request bodies or credentials.

## Real identity-provider integration checks

`python3 -m pytest -q tests/test_real_identity_integration.py` exercises the production JWT verifier against a disposable Keycloak realm with human and service identities, tenant/site restrictions, signing-key rotation, token expiry and provider outage/recovery. Install the development test dependencies first. Docker must be available, and both image digests from `ops/keycloak-compose.yml` must already be cached; missing prerequisites fail rather than silently skip these acceptance checks. The regular `make test-integration` and coverage gates include them.

The tests create uniquely named containers and a network, use a dedicated disposable identity PostgreSQL database, generate credentials in memory, and publish Keycloak on a dynamic loopback port. They do not read `ops/.env` or change an existing identity stack. The outage check suspends and resumes only the test-owned provider. Set `AVA_TEST_IDENTITY_EVIDENCE_DIR` to preserve redacted provider logs and lifecycle records at a chosen persistent location; the default is `results/identity-acceptance/`. Cleanup removes only test-owned resources, including on setup failure. These integration checks do not establish installed real-HTTP API acceptance or inference quality.

## Installed HTTP API acceptance

`make test-e2e` rebuilds an isolated `.venv-e2e` environment, installs the API test dependencies and AVA non-editably, then runs the real-HTTP API workflow. Set `PYTHON` to the development interpreter and `E2E_VENV` to an owned environment path when needed. Installation needs the declared Python packages; Docker and both pinned images from `ops/keycloak-compose.yml` must be available before the tests start.

The workflow uses a real disposable Keycloak realm, separate identity and event PostgreSQL containers, and a separate API process listening on a dynamic loopback port. It verifies package paths in both processes, migrates and seeds the database through the installed persistence adapter, and checks event pagination/metadata, human and service identities, tenant/site isolation, redaction and grant revocation/regrant. Source-checkout or editable imports, missing approved fixture bytes and missing required services fail the gate. Server startup and shutdown must complete the production application lifespan.

The committed event fixture contains approved sanitized records from real runs, including distinct run session aliases. This proves API handling of that data; it does not certify inference accuracy, original media availability or media delivery. Credentials are generated in memory, test configuration is passed to the server through stdin, and only test-owned processes/containers are cleaned up. `AVA_TEST_IDENTITY_EVIDENCE_DIR` controls persistent redacted server/provider logs, import provenance and lifecycle evidence. The existing packaged integration gate remains separate.

## Opt-in live stage measurements

Set `live.batch.profile_stages: true` in a batched run to include bounded common-window stage summaries in `metrics.json` and process/thread/host CPU deltas in resource samples. Profiling is disabled by default. The recorder excludes spans crossing readiness, warm-up or drain boundaries. By default `live.batch.profile_sample_every: 16` selects every sixteenth call per stage/camera; sparse persistence stages are always selected. Counts and totals describe selected complete-window spans, while `calls_all_phases` includes warm-up/drain calls and is not a measurement-window denominator. Percentiles use the most recent bounded selected samples, with truncation explicit. Set the positive integer sampling interval explicitly for controlled overhead comparisons.

Predictor preprocessing, inference and postprocessing are measured as host wall time; these spans can include transfers and synchronization. They are not isolated GPU kernel, H2D or D2H timings, and overlapping stages must not be summed as serial costs. Missing device timings remain unavailable. Measure profiling overhead against a matched disabled run before using these diagnostics for attribution. Process/thread CPU is sampled no more often than every 15 seconds, with actual delta interval endpoints reported; intervening resource records mark CPU sampling as not scheduled. Process/thread CPU uses 100% per full core and can exceed 100% for the process; host CPU is measured separately across all cores. No event samples means unavailable event latency.
