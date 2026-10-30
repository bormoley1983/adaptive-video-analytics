# ADR-14: AVA owns an independent control and observation API

Date: 2026-10-21 · Revised: 2026-10-22 · Status: Accepted for design; implementation pending · Scope: E-3–E-7, F-1–F-6, X-1/X-2.

## Context and decision

The independent AVA product needs an API and a small operator UI for capabilities
that its own runtime and evidence justify: live camera processing, spatial and
semantic rules, events, evidence, optional investigation/OCR/offline analysis
and worker health. The contract is derived from AVA domain behavior and measured
needs. It must not copy an external product's resources, endpoints,
authentication, ownership, storage, paging, time or delivery semantics.

AVA will expose a versioned, resource-oriented `/api/v1` contract over AVA
domain models. The API separates:

- desired configuration (sites, cameras, versioned profiles and assignments);
- observed processing state (sessions, workers, capacity and health);
- immutable analytic facts (events, observations and evidence);
- mutable operator workflow (incidents and acknowledgements);
- asynchronous work (source validation, searches, uploads, analyses and
  exports); and
- durable integration delivery (subscriptions, replay cursors and attempts).

REST is the authoritative command/query surface. A cursor-resumable WebSocket
is a low-latency projection, not the only recovery mechanism. Long operations
return jobs and never hold an HTTP request for model execution. API schemas use
UTC RFC 3339 timestamps, normalized source-image geometry, opaque identifiers,
explicit schema/resource revisions, idempotency keys for retried commands,
optimistic concurrency for updates and RFC 9457 problem details. Evidence is
accessed through authorized API responses or short-lived URLs; internal paths
and camera credentials never appear in responses.

Authentication is supplied by the deployment identity provider. Human and
machine identities use the same authorization model with tenant/site-scoped
roles. AVA does not implement a second identity directory. Keycloak is the
planned initial local provider in `ops/`; its F-1-S3 integration and operational
validation remain pending. Protected requests fail when a required signing-key
refresh cannot succeed. The identity port remains provider-independent. Storage, broker,
VMS, detector and third-party analytics details remain behind ports/adapters.
Optional consumer and provider adapters translate at the edge and retain
external IDs in namespaced bindings; their DTOs do not leak into AVA resources.

ADR-16 implements this boundary as a modular monolith with presentation,
application and domain layers plus infrastructure adapters. Routes invoke
focused application use cases. A separate facade is optional and is introduced
only when it removes demonstrated subsystem orchestration from callers; it is
not a mandatory fourth layer.

The normative design is [api-contract.md](../../design/api-contract.md).
The E-series remains the analytics/runtime and integration-milestone plan;
F-series stories implement the public API surface alongside it, and X-series
stories add the UI last. No accepted E-series task is cancelled by this ADR.

## Alternatives and consequences

Mirroring any existing API could be fast initially, but would import assumptions
that AVA has not chosen or validated. Implementing only one consumer contract
would not supply AVA's configuration, investigation or operations. A
GraphQL-first or event-bus-only boundary adds complexity without removing the
need for command semantics, media transfer and durable replay.

The chosen boundary needs translation code and explicit compatibility tests,
but lets AVA run its own pipeline and add replaceable consumers or providers
without changing the core contract. It also prevents a
realtime socket from being mistaken for durable delivery. Full functionality
is staged: unsupported capabilities are absent or reported by the capability
resource, never simulated by an adapter.

## Validation and revisit

F-1 publishes an OpenAPI document and executable schema/authorization/
idempotency tests. Later epics add end-to-end tests for replay, evidence,
search, offline analysis and failure recovery. X-1 and X-2 consume only the
public API. Validate adapters against owned stubs and, when access exists,
their real systems; a stub cannot certify a particular external integration.

Revisit the transport mix only for a measured client need, such as a Kafka
contract, high-volume gRPC worker traffic or resumable-upload protocol. Revisit
resource boundaries when face recognition, LPR, procedure reports or a general
VMS becomes approved scope; none is implied by this decision.
