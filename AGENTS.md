# AVA agent instructions

Applies to every model and client.

## Before work

1. Read `README.md`; inspect status and relevant files.
2. If `docs/` exists, read `docs/intro.md`, the active plan, applicable ADRs,
   `docs/tests_rules.md` and latest progress entry.
3. State one smallest observable outcome and its file scope. Verify assumptions.
4. Preserve unrelated work. Do not reformat, rename or overwrite active files.

## Boundaries

- Build the smallest working vertical slice. Add infrastructure only when active
  behavior requires it.
- Keep ingestion, inference, tracking, rules, events and measurement separated
  by explicit interfaces.
- Measure GPU availability, capacity, latency and accuracy. Do not stop shared
  workloads or change drivers without owner approval.
- Keep private docs, footage, weights, credentials and results out of Git and
  images. Commit only sanitized examples.
- Change architecture through an ADR when private docs are available.

## Parallel work

- Use one sibling worktree and branch per task:
  `../ava-worktrees/<task>` and `agent/<task>`. Never share a worktree.
- Create it from an explicit committed base. Other worktrees' uncommitted changes
  are not inherited.
- Assign each worker an outcome and owned files. Coordinate before scopes overlap.
- Workers make focused commits. The integrator reviews, merges or cherry-picks,
  resolves overlaps and runs combined validation.
- Ignored assets are not copied. Share `docs/`, `videos/` and `models/` read-only
  only when needed; keep `results/` local; never copy credentials.
- Only the integrator edits shared private progress/handoff docs. Workers report
  required doc changes in their handoff.
- Clean up only after integration or explicit abandonment and artifact recovery.
  Never force-remove another agent's work.

## Code and validation

- Prefer precise names and small functions. Comment only non-obvious intent or
  constraints. Keep planning IDs out of source comments, tests and docstrings.
- Validate configuration and public contracts before runtime work.
- Follow `docs/tests_rules.md`. Use the separate `make test-unit`,
  `test-integration`, `test-e2e`, `test-infra`, `test-coverage` and `lint` gates.
- Report exact commands and results. Unrun or unavailable is not passed. Mocked
  or CPU-only tests do not prove GPU, performance or infrastructure readiness.

## Context and handoff

- Keep one bounded subtask per agent session. Do not start unrelated work.
- Before clearing or compacting context, finish the current safe checkpoint and
  record: branch/base, changed files, behavior, commands/results, limitations,
  requested private-doc updates and the exact next action.
- Use a fresh session for a materially different subtask. Compaction may follow
  a durable checkpoint; it must not replace repository records or happen during
  an unfinished edit, test or merge.
- Update affected `README.md` files before completion. Update current progress
  and `docs/intro.md` only when handoff, milestone or repository settings change.
