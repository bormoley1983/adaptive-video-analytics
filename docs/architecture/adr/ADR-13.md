# ADR-13: risk-based 90% coverage gate

Date: 2026-10-21 · Status: Accepted · Scope: development validation.

## Context

ADR-11 established a 100% line and branch coverage gate. That target encourages
tests for every defensive or environment-specific branch, even when those tests
mainly reproduce implementation details rather than verify useful behavior. AVA
also contains GPU, model, video-codec and third-party adapter boundaries whose
real confidence comes from end-to-end and infrastructure tests.

Coverage remains useful as a broad signal, but it is not evidence that assertions
are meaningful and should not displace behavior, integration or measured GPU
validation.

## Decision

`make test-coverage` enforces at least 90% combined line and branch coverage for
the measured `ava` package. Changed public behavior still requires applicable
positive, negative, boundary and failure tests; reaching 90% does not excuse an
untested contract or known regression.

Do not add low-value tests solely to increase the percentage. Hardware-dependent
and third-party behavior belongs in the lowest suite that can exercise it
truthfully. Narrow coverage exclusions still require an explanation and nearest
owned-boundary validation when the omitted code represents material risk.

## Alternatives considered

- Keep 100%. This maximizes the metric but creates disproportionate maintenance
  and encourages brittle tests for low-risk defensive paths.
- Require 95%. This is reasonable for a mature deterministic library, but leaves
  too little room for AVA's current hardware and third-party adapter boundaries.
- Use no threshold. This removes metric-driven tests but allows broad untested
  regressions to accumulate without a visible gate.

## Consequences

The coverage gate remains demanding while test effort can focus on domain rules,
contracts and failure behavior. A passing percentage is necessary but not
sufficient: affected suites, lint and any required GPU or infrastructure checks
must still pass independently.

The lower threshold may hide a poorly tested critical path if reviewers consider
only the aggregate number. Reviews therefore inspect changed behavior and missing
lines, not just the final percentage.

## Validation and revisit

Run `make test-coverage` and confirm the report is at least 90% with branch
measurement enabled. Revisit the threshold after E-2 and after major adapter or
runtime changes; raise it only when the additional coverage represents durable
behavioral confidence rather than test coupling.

This ADR supersedes only ADR-11's 100% threshold and associated exception policy.
The layered suites and behavior-oriented test requirements remain accepted.
