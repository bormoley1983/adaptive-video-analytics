# ADR-15: local containerized operations and PostgreSQL persistence

Date: 2026-10-23 · Revised: 2026-10-23 (SeaweedFS and deferred Kafka) · Status: Accepted for staged implementation · Scope:
E-3, F-1, F-3, F-5 and E-6.

## Context

AVA has reached the point where multi-camera measurements, service health,
central logs and durable events matter. The owner has external PostgreSQL,
Prometheus and ELK services, but chose to operate the AVA learning environment
through project-owned Docker containers before validating external adapters.
The project should teach the complete service lifecycle without making the
video hot path depend on monitoring or adding every available datastore.

The implemented runtime writes JSONL events and local evidence. E-3-T4 is the
current P0 instrumentation task. F-3 already requires immutable events and a
transactional outbox, and F-5 requires metrics, audit, recovery and retention.
These needs require explicit ownership and failure boundaries.

## Decision

Use a separate CPU-only Docker Compose project for AVA's local operational
dependencies. Its first supported profile contains:

- PostgreSQL 18 for canonical configuration, event, incident, audit and outbox
  metadata;
- Prometheus for bounded-cardinality operational metrics;
- Grafana for provisioned metrics and log views;
- Loki for centralized logs; and
- Grafana Alloy for Docker log discovery, processing and forwarding.

The durable evidence profile later adds SeaweedFS as the selected S3-compatible
object store. It is staged with F-3-S2 after the PostgreSQL event/outbox slice,
not added to the P0 B-022 observability task. `LocalEvidenceStore` remains the
deterministic test/offline adapter; `S3EvidenceStore` targets SeaweedFS for the
service path. PostgreSQL stores evidence metadata and lifecycle state while
SeaweedFS stores bytes.

Add PostgreSQL-metrics and GPU/host exporters only when their compatibility and
required privileges are validated. Pin every adopted image by tested version and digest.
Persist service state in named volumes. Bind browser and management endpoints
to `127.0.0.1` by default. Inject development credentials through ignored
runtime configuration; never commit real credentials or expose the study stack
to the LAN by default.

PostgreSQL is the only database engine implemented for the service path. Core
code depends on narrow domain ports—event sink/repository, incident repository,
unit of work/outbox and evidence store—rather than PostgreSQL client DTOs. This
is separation of responsibilities, not a promise of interchangeable database
engines. Variable event/model fields may use validated `jsonb`; stable identity,
time, ownership, lifecycle and relationship fields remain typed columns with
constraints and indexes.

PostgreSQL becomes the canonical source of truth when B-023's vertical slice
passes. JSONL remains a portable benchmark/debug export and migration fixture;
it is not a second authoritative service database. Evidence bytes remain
outside database rows behind the evidence-store port. Local files are the
initial adapter and an S3-compatible adapter remains a later integration task.

AVA writes structured JSON logs to stdout. Required correlation fields are
defined in the operations design; credentials, bearer tokens and raw source
URLs are forbidden. Log collection is an external concern: the runtime does
not import Loki or Alloy clients. Loss or unavailability of the log stack must
not block inference or event persistence.

Do not add MongoDB or Redis now. MongoDB does not improve the relational event,
configuration, incident and outbox model enough to justify a second source of
truth. Redis has no measured caching, shared-session, rate-limit or coordination
requirement. Kafka is the preferred future event-stream adapter for retained,
replayable publication to multiple consumers, but remains inactive until a
named consumer, measured rate/retention requirement, partition-key decision and
outage acceptance exist. RabbitMQ remains only a candidate for bounded job
queues if job semantics are later required. OpenTelemetry is also deferred.
Availability or study value alone does not make a component an application
dependency.

All service adapters follow ADR-16's layered hexagonal boundaries. A dedicated
facade is optional, not an architectural layer or prerequisite.

The local stack supersedes ADR-9 only for the current development/study
deployment. ADR-9's external-service inventory and later adapter-validation
requirements remain valid for E-6. The local Compose project never starts,
stops, reconfigures or resets an external service.

## Alternatives and consequences

Reusing the existing external services immediately would reduce duplicate
runtime cost, but would skip local lifecycle, backup, failure and configuration
learning and keep network/access dependencies on the critical path. ELK is
capable and familiar to the owner; Loki and Alloy were selected to add a new
learning target and use one Grafana surface for metrics and logs. ELK remains a
future adapter evaluation, not a parallel default stack.

MongoDB offers flexible documents and change streams, but AVA still needs
cross-resource integrity, deduplication and atomic event/outbox writes.
PostgreSQL supplies those requirements while `jsonb` covers bounded variable
payloads. Redis could later provide a cache, distributed rate limit or
short-retention stream, but adding it now would introduce durability and
invalidation choices without an observed bottleneck.

The chosen stack consumes CPU, RAM and disk on the same host used for GPU
experiments. Run it as an independently controlled Compose project, declare
resource limits and retention, and measure its overhead separately. Monitoring
is evidence about the system, not proof that the monitored behavior passes.

## Validation and revisit

B-021 validates telemetry semantics and overload visibility. B-022 validates
container startup, persistence, scraping, log delivery, dashboards, localhost
exposure and observability-backend outage behavior. B-023 validates migrations,
constraints, duplicate delivery, atomic event/outbox commits, restart and
database outage behavior. B-024 validates health aggregation, alerts,
retention, backup and restore.

F-3-S2 validates the selected SeaweedFS operations, authorization, upload,
range-read, delete, access-link, retention and outage behavior against a
containerized instance before an external instance can be certified.

Revisit MongoDB only for an approved document workload that PostgreSQL cannot
serve acceptably. Revisit Redis only after a measured cache, distributed
session/rate-limit, lock or short-retention stream requirement. Revisit ELK for
a defined search/retention requirement or an explicit comparison study.
Activate Kafka only after its named-consumer and measured-volume gates pass;
use PostgreSQL outbox state until then rather than publishing from workers.
Revisit external services at E-6 after routing, TLS, authentication, namespace,
quota and ownership inputs in B-011 are available.

Implementation contracts: [operations and persistence](../../design/operations-persistence.md)
and [application architecture](../../design/application-architecture.md).
