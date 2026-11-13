# Test rules

These rules apply to production changes, tests, reviews and coding-agent sessions. [ADR-11](architecture/adr/ADR-11.md) records the decision.

## Commands and scope

| Command | Scope | Allowed dependencies |
|---|---|---|
| `make test-unit` | One function, method or class in isolation | Deterministic fixtures, fakes and temporary files; no network, models, GPU or services |
| `make test-integration` | Multiple AVA components or one adapter boundary | Local processes, fakes, temporary storage, explicitly controlled test doubles and test-owned services |
| `make test-e2e` | Complete workflow through the packaged CLI/API with real artifacts | Declared models, media and runtime; GPU when the workflow requires it |
| `make test-infra` | Image build, Compose/runtime, GPU, mounts and external-service contracts | The explicitly named host and infrastructure dependencies |
| `make test-coverage` | Unit and integration coverage of production Python | Unit/integration prerequisites, including declared test-owned services; at least 90% combined line and branch coverage |

Unmarked pytest tests are unit tests. Mark higher-level tests with exactly one primary marker: `integration`, `e2e` or `infra`. A test belongs at the lowest level that can prove the behavior. Do not duplicate an assertion at every level unless each boundary can fail independently.

An unavailable prerequisite means **not run**, not passed. Record the missing model, GPU, media, endpoint or credential. Never use a unit result to claim inference, container or infrastructure readiness.

## Arrange–Act–Assert

Each test has one observable behavior and three recognizable phases:

1. **Arrange:** build the smallest state and inputs that explain the case.
2. **Act:** perform one behavior under test.
3. **Assert:** verify the public result and externally visible side effects.

Prefer spacing and clear local names to `# Arrange`, `# Act` and `# Assert` comments. Add those comments only when the phases are otherwise ambiguous. Avoid asserting private implementation details, call order or incidental formatting unless they are part of a contract.

Name tests as behavior, for example `test_rejects_polygon_with_out_of_bounds_vertex`. Do not include roadmap, epic or task identifiers in test names, docstrings or code comments. The test should explain the behavior without planning context.

## Required behavior coverage

For every added or changed public function or method, identify and test all applicable cases:

- normal valid input and the returned result;
- each validation or rejection path;
- lower and upper boundaries, including empty and missing values;
- state transitions, retries, duplicate input and idempotency;
- raised errors and visible partial side effects;
- time discontinuities, ordering and concurrency where relevant;
- resource cleanup and recovery after failure;
- compatibility behavior promised to existing configurations or artifacts.

Positive and negative cases must be separate tests when they diagnose different failures. Parameterize cases that share the same behavior and assertion shape. The measured package must maintain at least 90% combined line and branch coverage, but the percentage does not replace meaningful behavioral tests for changed public methods and functions. A covered line without a contract assertion is not sufficient.

Generated adapters, defensive platform branches and hardware-only paths require a narrow exception in the relevant ADR or benchmark report when omitted coverage hides material risk. State why direct execution is impractical, validate the nearest owned boundary and name the suite that supplies the missing confidence. Do not add broad coverage exclusions or lower `fail_under` merely to make a failing task pass.

## Fixtures and reliability

- Use fixed clocks, seeds, identifiers and tiny synthetic inputs for deterministic suites.
- Keep shared fixtures behavior-neutral; scenario-specific values stay in the test.
- Unit tests require no network or external services. Integration tests may use declared, test-owned local services; do not download assets or contact uncontrolled external systems. The real-identity integration checks require Docker and the cached image digests declared in `ops/keycloak-compose.yml`.
- Keep secrets, private footage, model weights and generated results out of test fixtures and Git.
- Assert outputs and evidence before deleting temporary resources.
- Treat flaky tests as failures. Fix their clock, state or dependency boundary; do not add blind retries.

## Comments and readable code

Use clear domain names so code and tests document themselves. Comments explain only non-obvious intent, constraints or tradeoffs and stay short. Never reference task numbers in code comments; planning links belong in progress documents, ADRs or commit/PR context. Remove comments that restate the next line.

## Agent workflow

Follow [AGENTS.md](../AGENTS.md) for startup, approvals, checkpoints and handoff. Select the smallest suite that proves each changed boundary; apply the behavior and coverage requirements above to production changes.

Run affected suites and, for production Python or test changes, coverage and lint where prerequisites permit. For documentation-only changes, validate the changed documentation and references; runtime suites are required only when runtime behavior is affected. Report exact commands, outcomes, failures, skips and unavailable prerequisites. Unrun checks are not passed; required checks must pass or their exceptions be documented.
