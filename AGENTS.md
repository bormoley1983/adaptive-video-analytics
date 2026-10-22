# AVA agent instructions

Applies to every model and client.

## Before work

1. Read `README.md`; inspect status and relevant files.
2. If `docs/` exists, read `docs/intro.md`, the active plan, applicable ADRs,
   `docs/tests_rules.md` and latest progress entry.
3. State one smallest observable outcome and its file scope. Verify assumptions.
4. Hold a QA session with a human to surface and resolve every missing detail,
   decision, assumption and acceptance criterion. Record the answers and revise
   the plan; explicitly unresolved details are blockers, not agent defaults.
5. Present the resulting plan and wait for explicit human confirmation before
   editing files or taking implementation actions. The original task request is
   not plan confirmation, and an agent cannot provide or infer this approval.
6. Preserve unrelated work. Do not reformat, rename or overwrite active files.

## Mandatory human gates

- The plan-confirmation gate above applies to every task, including delegated
  and parallel work. If scope or assumptions materially change, repeat the QA
  and obtain confirmation of the revised plan before continuing.
- Never create, amend, merge, squash, cherry-pick or otherwise write a Git
  commit without a human approving that exact commit after reviewing its diff,
  validation results and proposed commit message. Approval is single-use; any
  material diff or message change requires fresh approval.
- Human gates are non-delegable. Silence, prior approvals, task instructions,
  test success and another agent's approval do not satisfy them. Agents stop at
  the gate and leave reviewable uncommitted changes when approval is absent.

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

## Tool-call discipline (dead-loop guard)

- Never repeat a tool call with identical parameters expecting a different
  result. If the same call returns the same output twice, stop retrying it.
- An empty or unchanged result is a finding, not a failure to re-poll. Act on
  it: change the query, broaden or narrow scope, read the file directly, or
  state the conclusion and move on.
- Before issuing any tool call, confirm it differs from the immediately
  previous one in at least one meaningful parameter (pattern, path, range).
  If it does not differ, do not issue it.
- Cap consecutive attempts at the same goal: after two distinct approaches
  yield no new information, report what was tried and what is unknown instead
  of continuing to probe.
- Never truncate or filter the output of a long-running or hard-to-reproduce
  command with an output-limiting pipe such as `| tail -N`, `| head -N`,
  `| grep ...`, `2>&1 | ...`, or any `sed`/`awk`/`cut` that discards lines.
  Builds, test runs, GPU workflows and installs can take minutes; limiting
  their output hides progress, masks mid-stream failures, and destroys evidence
  needed for review. Run the command so its full output is captured (let the
  terminal buffer it, or redirect to a log file). If you need to search or
  inspect a portion, do it as a separate step against the saved log file rather
  than discarding lines at the source.

## Parallel work

- Use one sibling worktree and branch per task:
  `../ava-worktrees/<task>` and `agent/<task>`. Never share a worktree.
- Create it from an explicit committed base. Other worktrees' uncommitted changes
  are not inherited.
- Assign each worker an outcome and owned files. Coordinate before scopes overlap.
- Workers prepare focused, reviewable changes and validation evidence. After
  the mandatory human commit approval, they may commit. The integrator obtains
  the same approval before any merge or cherry-pick that writes a commit, then
  resolves overlaps and runs combined validation.
- Ignored assets are not copied. Share `docs/`, `videos/` and `models/` read-only
  only when needed; keep `results/` local; never copy credentials.
- Only the integrator edits shared private progress/handoff docs. Workers report
  required doc changes in their handoff.
- Clean up only after integration or explicit abandonment and artifact recovery.
  Never force-remove another agent's work.

## Task completion and integration

- A task is not complete when its worker commit exists. It is complete only
  after the branch is pushed, reviewed through a merge request, explicitly
  approved by a human for merge, integrated into `develop`, and validated with
  the combined `develop` state.
- Agents must not merge their own work or infer merge approval from commit or
  plan approval. Fast-forward, merge-commit, squash and rebase merges all require
  explicit human confirmation immediately before updating `develop`.
- After an approved merge, synchronize the `develop` worktree, run the affected
  combined checks, and report the exact results. Remove the task worktree and
  branch only after integration is verified and artifacts are recovered.

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
