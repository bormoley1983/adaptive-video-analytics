# API acceptance evidence

Reviewed against develop `30544b8` on 2026-11-14. Statuses: **satisfied** (supported by merged implementation and recorded validation), **waived-with-record** (explicit human rerun waiver), **deferred** (non-blocking owner decision) and **open** (acceptance incomplete).

Implementation coordinates: schema `9f522b2`; schema/service shell PR #33 `b24d0f6`; identity PR #35 `bb498bc`; request identification `8a8c969`; mutations PR #41 `3087d2a`; camera/event reads PR #38 `adde889`; real-provider and installed HTTP acceptance MR #47 `f2b2dfd`, merged as `9512ccc`. The [capability summary](api-foundation.md) describes their resulting behavior. Private planning mappings and raw evidence are excluded under [ADR-10](architecture/adr/ADR-10.md).

Recorded MR #47 branch results: unit 712, integration 64, packaged 7, E2E 5, infrastructure 8, coverage 93.27% and lint passed. Completed develop reruns: unit 712, integration 64 and packaged 7. The human explicitly waived further develop E2E/infrastructure/coverage/lint reruns on 2026-11-08. These are historical records, not new runs; branch numbers are not develop passes. That waiver does not cover the open routing boundary.

## Contract and schemas

| Criterion | Evidence | Boundary | Status |
|---|---|---|---|
| OpenAPI 3.1 versioning, identifiers, UTC/source time, normalized geometry, cursor pages, jobs and RFC 9457 problems | Commits `9f522b2`, `b24d0f6`; [OpenAPI contract](../src/ava/api/openapi.yaml) | unit | satisfied |
| Examples validate; automated compatibility check rejects an intentional breaking change | [Schema checks](../tests/test_api_schema.py); recorded merge validation | unit | satisfied |

## Service shell and ports

| Criterion | Evidence | Boundary | Status |
|---|---|---|---|
| FastAPI/Pydantic presentation over ADR-16 use cases; liveness, readiness, version and capabilities | PR #33 `b24d0f6`; [route checks](../tests/test_api_app.py); seven recorded merge validation gates; required FastAPI/httpx dependencies now declared in `pyproject.toml` | unit/integration | satisfied |
| Transport models avoid detector/vendor/ORM/persistence DTO dependencies | Dependency-direction checks in [test_api_app.py](../tests/test_api_app.py) | unit | satisfied |
| Readiness distinguishes required, optional, degraded, stale and unknown dependencies without requiring Prometheus/Loki | [Readiness checks](../tests/test_api_app.py) | unit | satisfied |
| Separate operational acceptance: alert rules, telemetry retention, PostgreSQL backup/restore and notification routing | Merged PRs #50, #52 and #53 (`30544b8`); operational ownership remains separate from API acceptance | operations | satisfied under separate operational acceptance |

## Identity and authorization

The [real-provider module](../tests/test_real_identity_integration.py) has seven functions yielding nine cases. The [installed HTTP module](../tests/test_api_acceptance_e2e.py) has four functions yielding five cases. Counts were checked against merged source; this documentation review did not rerun them.

| Criterion | Evidence | Boundary | Status |
|---|---|---|---|
| Initial Keycloak deployment with provider-independent identity port | PR #35 `bb498bc`; [identity port](../src/ava/application/identity.py) and [JWT verifier](../src/ava/infrastructure/jwks_verifier.py) | unit/integration | satisfied |
| Issuer, audience, signature, expiry and scoped claims; fail closed on required key-refresh failure | PR #35 deterministic identity checks; MR #47 `f2b2dfd` audience/expiry corrections; production verifier requires non-empty audience and finite numeric expiry | unit | satisfied |
| Real human and service tokens on protected routes; health remains open | `tests/test_real_identity_integration.py::test_human_and_service_tokens_cross_protected_routes`; deterministic route checks cover unauthenticated health | integration/real provider | satisfied |
| Signing-key rotation preserves previous-token validity | `tests/test_real_identity_integration.py::test_rotated_signing_key_is_refreshed_without_losing_old_token` | integration/real provider | satisfied |
| Provider outage fails closed and recovers | `tests/test_real_identity_integration.py::test_provider_outage_fails_closed_and_recovers` | integration/real provider | satisfied |
| Genuine expiry, tamper and wrong-audience rejection | `tests/test_real_identity_integration.py::test_expired_provider_token_is_rejected`, `tests/test_real_identity_integration.py::test_tampered_real_token_is_rejected`, `tests/test_real_identity_integration.py::test_real_token_with_wrong_audience_is_rejected` | integration/real provider | satisfied |
| Installed API package and production lifespan | [Installed HTTP checks](../tests/test_api_acceptance_e2e.py) assert `installed_api_provenance`, non-editable imports and completed lifespan; packaged validation recorded seven passes on branch and develop | packaged/E2E host API process | satisfied |
| API-container-to-provider routing | The [acceptance bootstrap](../tests/api_acceptance_support.py) launches host `sys.executable` and reaches a loopback-published Keycloak container. The cited evidence does not establish an API process inside a container reaching its provider. Owner: domain/infrastructure owner; recommendation: separately approved bounded routing proof or explicit owner disposition. | infrastructure/container routing | **open** |
| Resource-level tenant/site isolation | `tests/test_api_acceptance_e2e.py::test_real_identity_without_camera_scope_cannot_discover_events` (other-tenant/other-site cases), `tests/test_api_acceptance_e2e.py::test_revocation_hides_history_and_regrant_restores_it` | installed HTTP E2E | satisfied |

The real-provider and installed-package evidence supersedes earlier pending provider statements. Installed host-process provenance does not close the separate container-routing boundary; no routing waiver is inferred from the 2026-11-08 rerun waiver.

## Request identity and concurrent mutations

| Criterion | Evidence | Boundary | Status |
|---|---|---|---|
| Request identification | Commit `8a8c969`; recorded unit/integration validation | unit/integration | satisfied |
| Idempotency storage; retries replay original results; body reuse conflicts | PR #41 `3087d2a`; [mutation contracts](../tests/test_mutation_contract.py), [PostgreSQL mutation checks](../tests/test_mutation_postgres_integration.py) | unit/real PostgreSQL integration | satisfied |
| Resource-specific revisions and stale-write rejection without partial mutations | Same PR #41 atomic mutation and ETag evidence | unit/real PostgreSQL integration | satisfied |

## Camera and event reads

| Criterion | Evidence | Boundary | Status |
|---|---|---|---|
| Application ports expose camera/event/evidence reads and cursor collections | PR #38 `adde889`; merged unit/integration validation | unit/integration | satisfied |
| Packaged retrieval of sanitized real events without local path or persistence DTO leakage; explicit unsupported capability | `tests/test_api_acceptance_e2e.py::test_approved_events_are_retrieved_over_installed_http_api`: 14 approved records, production persistence, pagination, request identifiers and redaction (`path`/`payload`/`model_ref` absent). Fixture SHA-256 `13eb3436cdb2ebc84344540dd785315961b6a873d847ff5d07121d32ca56780f` | installed HTTP E2E | satisfied |
| Optional camera sites, explicit tenant grants and current-grant control of historical access | `tests/test_api_acceptance_e2e.py::test_revocation_hides_history_and_regrant_restores_it`, `tests/test_api_acceptance_e2e.py::test_human_viewer_cannot_replace_the_trusted_grant` | installed HTTP E2E | satisfied |
| Movable-camera history and historical-location semantics | Deferred to a future approved use case; owner: domain owner | product decision | deferred, non-blocking |
| Site-less access for site-restricted callers | Shipped denial: `tests/test_access_policy.py::TestSiteScope::test_site_restricted_caller_denied_for_siteless_camera`, `tests/test_api_events.py::TestAuthAndScoping::test_site_restricted_caller_excludes_siteless_camera`, `tests/test_event_read.py::TestScoping::test_site_restricted_caller_excludes_siteless_camera`. Whether to permit access remains with the domain owner. | deterministic unit/API | shipped denial satisfied; product decision deferred, non-blocking |

## Validation record and acceptance result

Schema, authorization, errors, pagination and boundaries were validated at the levels in [test rules](tests_rules.md). MR #47 recorded the separate branch suites and completed develop reruns listed above; further develop E2E/infrastructure/coverage/lint reruns were explicitly waived on 2026-11-08. Historical raw logs remain private. No runtime suite was rerun for this documentation revision.

- **Open criteria: one.** API-container-to-provider routing remains incomplete. Owner: domain/infrastructure owner; recommendation: separately approved bounded proof or explicit owner disposition.
- **Deferred, non-blocking:** movable-camera history and the domain-owner decision on permitting site-less access for site-restricted callers.
- **Waived-with-record:** the named develop reruns after MR #47; this is not a routing waiver.
