# ADR-11: layered test harness and full behavior coverage

Date: 2026-10-20 · Reviewed: 2026-10-21 · Status: Accepted; coverage threshold superseded by [ADR-13](ADR-13.md) · Scope: development and delivery validation.

## Context

AVA combines deterministic domain logic with model inference, video processing, GPU containers and, later, external services. One undifferentiated test command either makes fast feedback depend on expensive infrastructure or gives a misleading result by testing only Python logic. Contributors and coding agents also need one shared definition of test scope and completion.

## Decision

Maintain four independently runnable suites:

- **Unit:** isolated deterministic behavior with no network, model download, GPU or service dependency.
- **Integration:** collaboration between AVA components or adapters using local fakes, fixtures or temporary storage.
- **End to end:** a complete user-visible workflow through the packaged entry point and real production-like boundaries.
- **Infrastructure:** container build/runtime, GPU exposure, mounts, external service connectivity and deployment contracts.

The repository exposes these suites as `make test-unit`, `make test-integration`, `make test-e2e` and `make test-infra`. Tests use pytest markers declared in `pyproject.toml`; unmarked tests are unit tests. An empty selected suite is not success: pytest's no-tests exit code remains visible.

Tests follow Arrange–Act–Assert, use behavior-oriented names and cover every changed public function or method with positive, negative, boundary and failure cases that apply. `make test-coverage` enforces 100% line and branch coverage for the measured `ava` package. Hardware-only or third-party paths may be excluded only through a narrow, documented exception with equivalent boundary validation; lowering the global threshold is not the exception mechanism.

The suite separation and behavioral-testing requirements remain accepted. ADR-13 supersedes only the 100% coverage threshold and its exception policy.

Detailed authoring, classification and agent workflow rules live in [tests_rules.md](../../tests_rules.md).

## Alternatives considered

- A single test command is simpler, but hides cost and environment differences and does not let local or CI workflows select the correct confidence level.
- Directory-only classification is visually obvious, but moving the active flat suite would conflict with ongoing feature work. Markers allow incremental adoption; category directories may still be used.
- A lower coverage threshold would make the current baseline easier to pass, but would turn missing behavior tests into accepted debt. The coverage command is an explicit gate so existing gaps stay visible until closed.

## Consequences

Fast unit feedback stays independent of GPU availability. Integration, end-to-end and infrastructure results can be reported separately with their prerequisites. New behavior carries a higher test-writing cost, while regressions and untested error paths become easier to identify. Until each non-unit suite contains collected tests, its command fails with pytest exit code 5 instead of producing a false green result.

## Validation and revisit

Run `make help`, `make test-unit`, each applicable higher-level suite, `make test-coverage` and `make lint`. Record commands, environment and actual outcomes; never infer one level from another. Revisit the tool choice if pytest markers no longer express the deployment topology, but preserve the four confidence levels and explicit coverage gate.

The 2026-10-20 audit could not run host `make test-unit` because the system
Python lacks pytest. In the existing runtime image with pytest installed, all
85 selected tests passed, but combined line/branch coverage was 68.39% and the
100% gate failed. Ruff 0.17.1 reported 44 findings. B-018 tracks restoration of
the accepted gate; these results are not a pass and do not invalidate the
passing behavioral tests.

## Narrow coverage exceptions

Hardware-only or third-party paths that cannot be exercised deterministically in
the unit/integration gate are excluded only by name, with the nearest owned
boundary validated instead. No broad exclusion or lowered `fail_under` is used.

- **`ava/detector.py` — model weight loading and inference.** The `load()`
  methods of `ConventionalDetector` and `PromptedDetector` (and the abstract
  `Detector.load`/`detect` stubs) construct real ultralytics `YOLO` models from
  multi-hundred-MB weights and warm a CUDA context. Direct execution is
  impractical in the unit gate: it requires GPU, model assets and network-free
  weight placement, which belong to the e2e/infra suites. The nearest owned
  boundary — mapping raw ultralytics-style results to normalised `Detection`
  records, device resolution (`_resolve_device`), and the load-required
  assertion — is validated by `tests/test_detector.py`. Real inference confidence
  is supplied by the E-1 benchmark runs (`benchmarks/e-1/report.md`) and the e2e
  suite. The uncovered lines are therefore the weight-loading/inference bodies,
  not owned logic.
