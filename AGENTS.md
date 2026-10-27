# AVA agent instructions

Applies to every model and client (Qwen single-agent sessions and Codex
multi-agent workers alike).

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
- Never commit directly to `develop`. All work lands on a task branch
  (`agent/<task>`), is reviewed through a merge request, and reaches `develop`
  only via an explicitly human-approved merge (fast-forward, merge-commit,
  squash or rebase). A direct commit already on `develop` must not be rewritten
  or amended; leave it and apply this rule from the next change onward.
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

Before EVERY tool call: never repeat a call identical to the immediately
previous one (same tool, same parameters); no retry is permitted. An empty or
unchanged result is a finding, not a failure — the next call must change at
least one meaningful parameter (pattern, path, range), or stop and state the
conclusion in plain text. After two distinct approaches yield no new
information, report what was tried and what is unknown instead of probing
further; three total attempts at the same goal is the hard maximum.

When a stall occurs: stop and report (calls, results, why unchanged) without
issuing another tool call in the same turn; classify the cause as environment
(state changed externally — re-read with a different query), query mismatch
(broaden or narrow one parameter, retry once) or genuine absence (state "X is
not present in Y" and move on); before any retry targeting the same goal, name
the specific parameter or precondition that changed since the last attempt —
if none exists, the call is prohibited. If the retry also returns an unchanged
result, stop entirely, report the full sequence and wait for direction.

Never truncate or filter the output of a long-running or hard-to-reproduce
command with an output-limiting pipe (`| tail -N`, `| head -N`, `| grep ...`,
`2>&1 | ...`, or any `sed`/`awk`/`cut` that discards lines). Builds, test runs,
GPU workflows and installs can take minutes; limiting their output hides
progress, masks mid-stream failures and destroys evidence needed for review.
Capture full output (terminal buffer or log file); inspect a portion in a
separate step against the saved log.

## Parallel work

- One sibling worktree and branch per task: `../ava-worktrees/<task>` and
  `agent/<task>`, created from an explicit committed base; never share a
  worktree. Other worktrees' uncommitted changes are not inherited.
- Assign each worker one outcome and owned files; coordinate before scopes
  overlap. Workers prepare focused, reviewable changes and validation evidence,
  then commit only after the mandatory human approval of that exact commit.
- Ignored assets are not copied. Share `docs/`, `videos/` and `models/`
  read-only only when needed; keep `results/` local; never copy credentials.
- Only the integrator edits shared private progress/handoff docs; workers
  report required doc changes in their handoff. The integrator obtains the same
  human approval before any commit-writing merge or cherry-pick, resolves
  overlaps and runs combined validation.
- Clean up only after integration or explicit abandonment and artifact
  recovery. Never force-remove another agent's work.

## Task completion and integration

- A task is complete only after its branch is pushed, reviewed through a merge
  request, explicitly approved by a human for merge, integrated into `develop`,
  and validated with the combined `develop` state — not when the worker commit
  exists. Agents must not merge their own work or infer merge approval from
  commit or plan approval.
- After an approved merge: synchronize the `develop` worktree, run the affected
  combined checks, report exact results. Then confirm the branch tip is an
  ancestor of `origin/develop` (`git merge-base --is-ancestor <branch>
  origin/develop`) and its worktree has no uncommitted changes; remove the
  worktree (`git worktree remove <path>`), delete the local branch (`git branch
  -d <branch>`) and the remote branch (`git push origin --delete <branch>`).
  Safe `-d` deletes only; never force-remove a branch that is not fully merged.
  Prune stale refs with `git fetch origin --prune`.

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
