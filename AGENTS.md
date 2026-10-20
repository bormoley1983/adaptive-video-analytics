# AVA agent instructions

These instructions are model- and client-independent. Apply them when working through local Ollama/LiteLLM routes, Copilot, Claude Code or OpenAI Codex.

## Read before changing files

1. Read `README.md` and inspect the working tree.
2. If the private `docs/` tree is present, read `docs/intro.md`, the current development plan, applicable ADRs, `docs/tests_rules.md` and the latest progress entry.
3. Preserve unrelated and concurrent edits. Do not rewrite another agent's active files merely to reformat or rename them.
4. State the smallest observable outcome and validate assumptions against the repository before implementing.

## Implementation boundaries

- Build the smallest working vertical slice. Do not introduce services or infrastructure before an active behavior needs them.
- Keep ingestion, inference, tracking, rules, events and measurement separated by small explicit interfaces.
- Treat GPU availability, camera capacity, latency and accuracy as measured facts, never assumptions. Do not stop shared GPU workloads or modify shared drivers without owner approval.
- Keep private documentation, footage, model weights, credentials and generated results out of Git and container images. Commit only sanitized configuration examples.
- Change an architectural decision through an ADR when the private docs are available; do not silently diverge.

## Readable code and comments

- Choose precise domain names and small functions so the code explains the behavior.
- Add short comments only for non-obvious intent, constraints or tradeoffs. Do not narrate the code.
- Do not reference epic, task or backlog numbers in source comments, test names or docstrings. Put planning traceability in progress documents, ADRs or change descriptions.
- Keep configuration and public contracts explicit and validated before runtime work begins.

## Tests and validation

- Follow `docs/tests_rules.md` when it is available. Use Arrange–Act–Assert and cover positive, negative, boundary and failure behavior.
- Use `make test-unit`, `make test-integration`, `make test-e2e` and `make test-infra` for their distinct confidence levels. Use `make test-coverage` for the full line and branch coverage gate and `make lint` for static checks.
- An unrun or unavailable suite is not a pass. Report the exact command, environment, result and missing prerequisite.
- Do not claim GPU inference, performance or infrastructure readiness from mocked or CPU-only tests.

## Handoff

Summarize changed files, observable behavior, validation results, known limitations and the exact next action. When private docs are available, update the current progress record and `docs/intro.md` if the handoff, active milestone or repository-wide settings changed.
