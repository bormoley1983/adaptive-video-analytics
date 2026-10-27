# ADR-9: reuse existing external infrastructure incrementally

Date: 2026-10-20 · Revised: 2026-10-23 · Status: External reuse retained for integration; local development superseded by ADR-15 · Scope: E-1–E-7.

## Context and decision

The owner reports existing external instances of PostgreSQL 18 with pgvector, Redis, Kafka, ELK, MongoDB, RabbitMQ, Qdrant, n8n, SeaweedFS S3 and Prometheus. Availability is inventory supplied by the owner, not a successful connectivity test. Versions other than PostgreSQL's major version, endpoints and credentials are not yet supplied.

Run AVA components in containers and connect to these services when required by
external integration. [ADR-15](ADR-15.md) supersedes the earlier no-duplicate
rule for the local study environment: AVA now owns a bounded local PostgreSQL,
Prometheus/Grafana and Loki/Alloy Compose stack so its lifecycle and failures can
be learned safely. This does not authorize changes to shared services or imply
that every available product is an AVA requirement. External component/client
selection must respect the existing service APIs and operator-controlled
upgrade schedule.

| Existing service | Proposed AVA role / activation point |
|---|---|
| PostgreSQL 18 | Later external adapter target for the same canonical event/config metadata and transactional outbox first implemented locally under ADR-15 |
| pgvector | Available extension; adopt only if embedding retrieval becomes a requirement |
| SeaweedFS S3 | Evidence object storage in E-6; mounted local evidence remains sufficient for E-1/E-2 |
| RabbitMQ | Bounded durable verification jobs in E-6 when asynchronous verification is adopted |
| Prometheus | Later external scrape target after the B-021/B-022 local Prometheus contract is validated |
| ELK | Deferred comparison/external log target; local development uses Loki/Alloy unless a defined ELK requirement is approved |
| Kafka | Available event streaming/replay integration; enable only for an agreed downstream consumer/contract |
| Redis | Available cache/transient coordination; no default dependency or canonical event state |
| MongoDB | Available document store; no current need for a second metadata source of truth |
| Qdrant | Available vector search alternative; evaluate against pgvector only for a defined retrieval workload |
| n8n | Optional downstream automation consuming published events; keep outside inference/rule correctness and critical latency paths |

These are planned external roles, not configured connections. RabbitMQ jobs and
Kafka event streams have distinct potential purposes; avoid duplicating
publication unless a consumer requirement calls for it. No vector database,
Redis, MongoDB or automation workflow is required for the current local stack.
ADR-15 records their explicit revisit criteria.

## Configuration and ownership

Inject endpoint/secret references privately at runtime. Use dedicated AVA accounts, schema/database, S3 bucket/prefix, broker vhost/queues/topics and log/metric labels as applicable. Validate routing/DNS, TLS trust, authentication, permissions, exact server/client versions and limits from inside the actual AVA container. An external service is not reached through the container's own `localhost`.

Confirm retention, storage quotas, cleanup permissions, backup ownership and outage behavior before pilot integration. Separate event metadata commits from evidence upload state; retry failed uploads without inventing successful evidence references. Test the required S3 upload/read/delete and access-link behavior against the actual SeaweedFS configuration. Do not assume every AWS feature is available or configured.

Compose lifecycle operations affect AVA-owned containers only. Provisioning AVA resources inside shared infrastructure must be scoped and documented; never reset shared databases/queues/buckets to clean a test. B-011 tracks access and readiness.

## Alternatives and consequences

A self-contained duplicate infrastructure stack would add maintenance and consume resources on the GPU host. Using every available service would delay the PoC and introduce overlapping state. Incremental reuse reduces deployment scope but makes network access, service ownership and failure testing explicit dependencies for later epics. E-1/E-2 remain runnable without infrastructure access.

## Validation and revisit

E-3 validates metric/log integration when activated. E-6 validates PostgreSQL migrations/outbox, evidence access/retention and adopted broker semantics, including outages and restarts. Revise roles when downstream requirements demonstrate a need; infrastructure inventory alone does not choose the workload architecture.

References checked 2026-10-20: [PostgreSQL 18 documentation](https://www.postgresql.org/docs/18/index.html), [SeaweedFS S3 API](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API). Instance compatibility and access remain untested.
