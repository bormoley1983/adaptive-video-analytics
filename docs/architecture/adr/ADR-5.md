# ADR-5: versioned events and staged persistence/integration

Date: 2026-10-19 · Revised: 2026-10-20 (E-2 contract review) · Status: JSONL contract implemented; runner evidence incomplete; pilot services proposed · Scope: E-2/E-4/E-6.

## Context and decision

Use one domain event contract across processing backends. Minimum fields: schema version, stable event ID, camera/session/track/rule IDs, rule/config/model/profile versions, object class, detector confidence, source timestamp, event UTC when known, emitted UTC, evidence reference, and primary/verification status. Correlate later verification with the original event; preserve the original primary alert. Detector confidence is not calibrated event probability, and VLM certainty is a separate field with evaluation limits.

Start with append-only JSONL and local evidence files. A single writer per file avoids multi-worker write races; aggregate through a separate sink. Under [ADR-15](ADR-15.md), PostgreSQL 18 becomes the only canonical service database after the B-023 event/outbox vertical slice passes; JSONL remains a benchmark/debug export and migration fixture. Use the local containerized PostgreSQL instance for development and validate the external instance separately at E-6. Store video outside database rows behind an evidence-storage interface: a mounted local directory first, then the existing SeaweedFS S3 service for integration. Validate the required S3 operations and access/retention behavior on the actual instance. Keep upload state explicit so an event cannot claim available evidence before upload succeeds. Expose evidence through authorized endpoints rather than filesystem paths.

For durable async verification, RabbitMQ remains a candidate only if a bounded
job workload is approved; use publisher confirms, consumer acknowledgements,
bounded retries and dead-letter/expiry policy. Kafka is the preferred future
adapter for retained/replayable domain-event publication to independent
consumers, but it is not active until ADR-15's activation gates pass. Both paths
preserve event identity and require idempotent consumers. Use a bounded
in-process or PostgreSQL-backed work path only when its process-loss semantics
are explicit. Queue length and stale-job age must be capped regardless of
transport. Neither broker is an unconditional dependency for REST-only
integration.

## Alternatives and consequences

JSONL is simplest for demos but provides no transactional delivery guarantee.
PostgreSQL/outbox adds operational cost but keeps state/publication recoverable.
Kafka is already available externally and is the selected future stream
adapter once its activation gates pass; RabbitMQ remains only a candidate job
transport. Do not mirror every message to both by default. Full infrastructure
roles are in [ADR-9](ADR-9.md). Do not claim exactly-once delivery:
reconnects/retries produce at-least-once behavior on the durable path, and
consumers must deduplicate. Define disk-full and outage handling; critical
events cannot be silently dropped under a 'durable' label.

## Validation and revisit

E-2 unit/integration fixtures validate event fields, deterministic IDs,
deduplication and evidence-store writes. The real runner did not materialize its
configured snapshots because the triggering frame enters the buffer after event
handling; clip evidence is pre-event rather than centered, and the event writer
appends across reruns. B-012 tracks these acceptance gaps. E-6 tests outage,
replay, restart, duplication, expiry, disk limits and authorized evidence access
against the required integration. Revisit for VMS-specific delivery guarantees,
evidence retention or multiple sites.

Sources checked 2026-10-19: [FastAPI documentation](https://fastapi.tiangolo.com/), [RabbitMQ acknowledgements and publisher confirms](https://www.rabbitmq.com/docs/confirms). These support component capabilities; the AVA durability design above remains a proposal to implement and test.
