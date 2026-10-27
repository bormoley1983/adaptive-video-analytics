# ADR-16: modular monolith with layered hexagonal architecture

Date: 2026-10-23 · Status: Accepted for staged implementation · Scope:
E-3, F-1–F-6 and X-1/X-2.

## Context

AVA is currently one Python package and deployable CLI with useful module
separation, but orchestration code constructs concrete JSONL, filesystem and
metrics implementations. The planned API, PostgreSQL persistence, SeaweedFS
evidence storage and possible later event publication would deepen those
dependencies if added directly to runners or route handlers.

AVA needs stable domain boundaries before the API and durable adapters arrive.
It does not yet need independently deployed services, network calls between
internal modules or a framework of interchangeable plugins. Layering,
hexagonal ports and a facade can overlap; stacking all three mechanically would
create forwarding classes rather than useful separation.

## Decision

Keep AVA as a modular monolith. Organize dependencies around three business
layers with infrastructure outside them:

1. **Presentation** owns FastAPI/CLI transport, authentication context,
   request/response DTOs and error mapping.
2. **Application** owns focused commands, queries, use-case handlers,
   transaction boundaries and orchestration of domain ports.
3. **Domain** owns event, rule, incident, evidence-metadata and lifecycle
   semantics without framework, SQL, S3, telemetry or broker types.
4. **Infrastructure adapters** implement outbound ports for PostgreSQL, JSONL,
   filesystem, SeaweedFS S3, Prometheus and later Kafka.

The fourth item is an adapter ring, not a fourth business layer. Dependencies
point inward: presentation and infrastructure depend on application/domain
contracts; the domain never imports FastAPI, database clients, AWS SDKs,
Prometheus/Loki clients or broker clients.

Use focused application services or command/query handlers as inbound ports.
A separate facade is not mandatory. Add a subsystem facade only when multiple
use cases must be presented as one stable coarse-grained contract and the
facade removes real orchestration knowledge from callers. Do not add a global
`AvaFacade` or a route→facade→service→handler chain whose layers merely forward
the same arguments.

Adopt these patterns as their owning behavior is implemented:

- repository and Unit of Work for durable domain state and transactions;
- transactional outbox for atomic event/publication intent;
- DTO/domain mapping and anti-corruption adapters at external boundaries;
- a composition root in each executable entrypoint for selecting concrete
  adapters;
- explicit state machines for evidence, incidents, jobs, sessions and delivery
  attempts;
- strategy adapters for detector, evidence-store and delivery implementations;
- idempotent consumer/inbox only when a broker consumer is introduced; and
- bounded timeout, retry, circuit-breaker and bulkhead behavior for remote
  dependencies.

CQRS remains lightweight: commands and queries may use different application
models, but PostgreSQL remains one canonical database until measured query
requirements justify a separate projection store. Full event sourcing, sagas,
microservices, database-engine portability, a service mesh and a generic plugin
framework are not current architecture.

Implementation proceeds by extracting seams around active behavior, not by a
wholesale directory move. The first proving slice makes live runners depend on
event-sink and runtime-metrics ports while retaining current JSONL and report
adapters. B-023 then adds PostgreSQL behind proven persistence ports. F-3-S2
adds SeaweedFS behind the evidence-store port. Entry points remain composition
roots and public behavior remains compatible through each extraction.

## Alternatives and consequences

Keeping direct concrete imports is initially shorter but would couple API,
storage and monitoring decisions to the analytics hot path. Splitting services
now would add deployment, consistency, tracing and failure modes before any
independent scaling requirement exists. A mandatory facade layer can hide a
complex subsystem, but at the current size it would duplicate application
services.

The modular monolith preserves simple deployment and in-process calls while
making process boundaries possible later. It requires disciplined import rules,
domain DTO mapping and dependency injection at entrypoints. Some adapter
boilerplate is intentional; forwarding-only abstractions are not.

## Validation and revisit

Validate dependency direction with import/architecture tests once packages are
split. Each extracted port must have at least one real adapter and behavioral
tests at the lowest proving level. API tests call application ports rather than
database implementations. PostgreSQL/SeaweedFS tests run as integration or
infrastructure suites under `tests_rules.md`.

Revisit a facade when several clients duplicate coordination across the same
use cases. Revisit process/service separation only after measured independent
scaling, isolation, ownership or deployment requirements. Revisit CQRS storage
only after indexed PostgreSQL queries fail an agreed target. Revisit sagas only
after one business transaction truly crosses independently owned services.

Implementation contract: [application architecture](../../design/application-architecture.md).
