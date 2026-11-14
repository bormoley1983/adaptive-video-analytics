# API foundation

Status: implemented; the remaining container-to-provider routing criterion passed on 2026-11-14 using the routing candidate based on develop `03d1fcb23f2c4c3c6a58d6e2b92ef18f634848a6`. Integration and broader candidate validation remain separate gates.

## Implemented scope

The versioned `/api/v1` slice exposes build and capability information, caller identity, camera state, events and evidence metadata through application ports. It supports authenticated event reads and transactional administrative mutations. Search, worker orchestration and a product UI are outside this foundation.

The architectural boundaries are recorded in [ADR-14](architecture/adr/ADR-14.md), [ADR-15](architecture/adr/ADR-15.md) and [ADR-16](architecture/adr/ADR-16.md). The [acceptance evidence](api-acceptance.md) maps each capability to public code, tests and commit coordinates.

## Contract and service health

The [OpenAPI contract](../src/ava/api/openapi.yaml) defines identifiers, UTC/source time, normalized geometry, cursor pages, jobs and RFC 9457 problems. [Schema checks](../tests/test_api_schema.py) validate examples and reject an intentional breaking change. Schema changes were introduced by `9f522b2` and PR #33 (`b24d0f6`).

FastAPI/Pydantic adapters expose liveness, readiness, version and capabilities over focused application use cases. [Route and readiness checks](../tests/test_api_app.py) enforce dependency direction and distinguish required, optional, degraded, stale and unknown dependencies. Prometheus and Loki availability are not runtime prerequisites. The shell merged in PR #33 (`b24d0f6`); `fastapi>=0.115` and `httpx>=0.27` are declared in `pyproject.toml`.

## Identity and access

Keycloak is the initial deployment identity provider behind a provider-independent [identity port](../src/ava/application/identity.py). The production [JWT verifier](../src/ava/infrastructure/jwks_verifier.py) validates issuer, audience, signature, finite expiry and scoped human/service claims, failing protected requests if a required key refresh fails. The initial implementation merged in PR #35 (`bb498bc`); mandatory audience/expiry corrections merged in MR #47 (`f2b2dfd`).

[Real-provider checks](../tests/test_real_identity_integration.py) cover human/service tokens, camera scope, tamper/audience rejection, rotation with previous-token validity, genuine expiry and outage/recovery. Seven functions yield nine parametrized cases. [Installed HTTP checks](../tests/test_api_acceptance_e2e.py) contain four functions yielding five cases and prove non-editable package execution, production lifespan and protected event retrieval with the real verifier.

The installed HTTP workflow runs the API as a host Python subprocess and reaches a containerized provider over loopback. The separate [container routing check](../tests/test_api_container_infra.py) now proves a CPU-only, non-editably installed API container authenticates real human/service requests through the production verifier to disposable Keycloak over an owned Docker network. Issuer agreement, exact provider DNS/address, installed hashes, open health, missing-authentication denial and production lifespan completion are asserted. This satisfies the container-routing criterion without establishing production TLS or deployment readiness.

## Request identity and concurrent mutations

Request identification merged as `8a8c969`. PR #41 (`3087d2a`) added atomic idempotent mutations, resource-specific ETags and stale-revision protection. Retries return the original result; key/body conflicts and stale revisions fail without partial writes. Evidence is in the [mutation contract checks](../tests/test_mutation_contract.py) and [real-PostgreSQL mutation checks](../tests/test_mutation_postgres_integration.py).

## Camera and event reads

Camera/event resources and cursor collections merged in PR #38 (`adde889`). Current trusted tenant grants govern historical access; static cameras may have optional sites. Installed HTTP acceptance in MR #47 retrieves the approved sanitized 14-record fixture through production persistence, with pagination, redaction, tenant/site denial and revocation/regrant. Fixture SHA-256: `13eb3436cdb2ebc84344540dd785315961b6a873d847ff5d07121d32ca56780f`.

Movable-camera history is deferred to a future approved use case. Whether site-restricted callers should ever access site-less cameras is a deferred, non-blocking domain-owner product decision. Shipped behavior denies that access; direct evidence is in the [access policy checks](../tests/test_access_policy.py), [API scoping checks](../tests/test_api_events.py) and [application read checks](../tests/test_event_read.py).

## Recorded validation and limits

MR #47 candidate `f2b2dfd`, merged as `9512ccc`, recorded unit 712, integration 64, packaged 7, E2E 5, infrastructure 8, coverage 93.27% and lint passed. Completed develop reruns recorded unit 712, integration 64 and packaged 7. The human explicitly waived further develop E2E, infrastructure, coverage and lint reruns on 2026-11-08. Branch results are not develop passes; the waiver did not cover the separate container-routing proof.

The real-provider and installed HTTP evidence resolves earlier pending validation statements. The new routing infrastructure check passed one case; focused helper/provenance unit regressions passed 19 cases. The routing evidence records exact owned-container/network cleanup. These new results are candidate evidence on the stated committed base, distinct from the historical merge results and rerun waiver. Separate operational acceptance covers alerts, retention and backup/restore; this review does not certify inference quality, media availability or capacity. Historical raw logs and planning mappings remain private under [ADR-10](architecture/adr/ADR-10.md).
