# AVA agent instructions

Applies to every model and client. This file owns operational workflow; [test rules](docs/tests_rules.md) own validation policy.

## Start and scope

- Inspect status, relevant files and `README.md`. Read existing `docs/intro.md`, the current task checkpoint, applicable ADRs, `docs/tests_rules.md` and latest progress once when relevant. Report missing context once; block only decisions that depend on it. Preserve unrelated edits without reformatting, renaming or overwriting them.
- Define the smallest observable outcome, owned files, dependencies and acceptance criteria. Split large work into ordered bounded deliverables; do not add infrastructure without active behavior requiring it.
- Read-only discovery, review and delegation may precede plan approval. Ask only unresolved questions; resolved answers need no reconfirmation. Implementation follows the human gates below.

## Human gates

1. **Plan:** combine resolved QA, recommendation, scope, validation gates, guardrails and integration path in one approval-ready plan. Obtain explicit human confirmation before implementation; do not separately seek explanation, recommendation or policy approval. One confirmation authorizes that scope, including delegated assignments. Unresolved decisions block dependent work; material scope or assumption changes require affected QA and one revised-plan confirmation.
2. **Commit/history:** obtain single-use approval of the exact diff, validation results and proposed commit message before committing or rewriting history. A material diff/message change requires fresh approval. Never commit directly to `develop` or amend/rewrite a direct commit already there.

Human gates cannot be delegated or inferred from silence, task requests, other-scope approvals, tests or agent approval. At a gate, name the required decision and reason, link precise scope/diff/evidence and limitations, give numbered review steps and approval/change options, and state what is blocked and happens next. Continue independent authorized work. Prepare reviewable work first; do not ask humans to perform safe agent work or approve provisional evidence as a quality baseline.

## Delegation and isolation

- Split investigations and implementation into bounded assignments. Without human prompting, delegate multi-file/directory investigations, substantial-output commands and independent routine discovery, reads, searches or validation. Use the configured delegate model when available; batch related commands rather than spawning per trivial operation. Sequential delegation is useful even without parallel work.
- Before handling qualifying work directly, name the specific blocker: unavailable tools, higher-priority prohibition, exhausted capacity or a concrete dependency/access constraint. Small size, convenience and lack of parallel work are insufficient; a targeted assignment-defining read may remain with the main agent.
- Give each worker one outcome, owned scope, relevant inputs, acceptance criteria, dependencies/open decisions, guardrails and expected evidence. Fit assignments to actual worker context limits. Record implementation assignments in the confirmed checkpoint; record pre-plan discovery assignments in the resulting plan if implementation follows.
- Require concise findings with paths/lines, examined scope, limitations, missing evidence and validation commands/statuses/log paths. No raw-document dumps. Search absence proves only the examined scope; synthetic/local evidence cannot establish unexamined repository or environment facts.
- Do independent work while workers run; otherwise wait. Never duplicate pending investigation or decide matters dependent on it. Send new investigative questions to the worker first; direct investigation requires a stated blocker above. The main agent retains decisions, human gates and evidence assessment, and may explain and perform a targeted acceptance check after the worker responds.
- Read-only workers and tests may share a checkout if they do not modify shared state. Write owned logs/artifacts to distinct paths; isolate tests that mutate shared files/services. Each implementation task, including main-agent work, uses a persistent `$HOME/ava-worktrees/<task>` worktree and `agent/<task>` branch from an explicit committed base, never shared or under `/tmp`. Other worktrees' uncommitted changes are not inherited.
- Coordinate overlapping scopes. Do not copy ignored assets or credentials; share needed `docs/`, `videos/` and `models/` read-only and keep `results/` local. Only the integrator updates shared private progress/handoff documents, resolves overlaps and runs combined validation. Workers report requested document updates.

## Safeguards and code

- Build the smallest vertical slice. Separate ingestion, inference, tracking, rules, events and measurement through explicit interfaces. Record architectural changes in an ADR when private docs are available.
- For GPU/runtime work, measure availability, capacity, latency and accuracy relevant to the claim. Never stop shared workloads or change drivers without owner approval.
- Keep private docs, footage, weights, credentials and results out of Git and images; commit only sanitized examples. Delegation never relaxes scope, privacy or shared-workload safeguards.
- Use precise names and small functions; comment only non-obvious intent/constraints. Keep planning IDs out of source comments, tests and docstrings. Validate configuration and public contracts before runtime work.
- Write each Markdown prose paragraph on one source line; preserve structural breaks. Leave unrelated formatting as documentation debt. Update affected READMEs; update progress/intro only for handoff, milestones or repository-setting changes.

## Tool-call discipline (dead-loop guard)

Before EVERY tool call, identify the goal/target, count prior attempts on that question and check existing evidence. Never repeat the immediately previous call with the same tool and parameters. All queries and saved-output inspections share a three-attempt maximum per goal/target; changing tools, delegates, command spelling, filters, patterns or windows does not reset it. Stop after two attempts yield no new information, or as soon as authoritative evidence resolves the question (e.g. pytest collection exits 0 and reports collected tests).

A no-match result ends that scoped search: report the examined scope and unknowns without inferring broader absence or retrying with another filter/window. On a stall, report calls, results and why no new information emerged; classify the cause as environment, query mismatch or genuine absence. Make no further call for that goal/target in the same turn. Reopen only after a material evidence/environment change: name it and explain why it reopens the question. Query, filter, tool or window changes alone do not renew the budget. If the renewed investigation yields no new information, stop, report the full sequence and wait for direction.

For long-running or hard-to-reproduce commands (builds, tests, GPU workflows, installs), never discard output through limiting pipes/filters such as `tail`, `head`, `grep`, `2>&1 | ...` or line-discarding `sed`/`awk`/`cut`; this hides progress, failures and review evidence. Capture full output once in a terminal buffer or persistent log and retain the producer's exit status. Inspect saved output separately within the same goal/target budget; do not rerun the producer for inspection.

Distinguish producer, downstream-filter and overall-pipeline exit statuses: `grep` exit 1 means no matches, not pytest failure. Report an uncaptured producer status as unknown; never infer producer failure from filter/pipeline status. Reuse captured authoritative evidence and stop when it resolves the question.

## Validation

Select affected checks under [docs/tests_rules.md](docs/tests_rules.md); its separate unit, integration, end-to-end, infrastructure, coverage and lint gates prove different boundaries. Record exact commands, exits, failures, skips and unavailable prerequisites. Unrun is not passed; mocked/CPU-only checks do not establish GPU, performance or infrastructure readiness.

## Checkpoints and handoff

Before implementation, persist one private `.md` checkpoint per bounded deliverable under `docs/dev-plan/`; use this schema instead of repeating policy:

- outcome, approval, scope/owned files, dependencies, acceptance criteria, guardrails and unresolved decisions;
- branch/committed base and assignments;
- ordered gates with current status, exact commands/results and persistent evidence paths;
- changed/untracked files, observable behavior, limitations and requested private-document updates at handoff;
- recovery-patch path and exact next action.

Update after each gate and before handoff so resumption needs neither chat history nor `/tmp`. Keep private evidence out of Git; checkpoints reference evidence by path. Save a current recovery patch including new files before handoff. Use persistent storage for plans, evidence and recovery; `/tmp` is disposable scratch only.

Finish and validate one deliverable at a time, then checkpoint. Start fresh for materially different work or context pressure; no forced reset between related deliverables. Never interrupt an edit, test or merge to compact; record interrupted state before resuming. Clean caches only when supported and understood, preserving project files, dependencies, evidence, worktrees and recovery artifacts. Do not start unrelated work.

## Review and integration

A read-only review finishes with findings; an editing step is ready for human review after affected checks and checkpointing. Leave changes uncommitted at the commit gate. Repository integration is complete only after push, merge-request review into `develop` and affected checks on the combined state.

After exact task-commit approval, the confirmed integration path needs no extra routine approval: integrate only the expected PR head with required checks passing and a mergeable PR. Stop for conflicts, failing checks or material scope changes.

After integration, synchronize the `develop` worktree and report affected combined-check results. Verify `git merge-base --is-ancestor <branch> origin/develop` and a clean task worktree before `git worktree remove <path>`, `git branch -d <branch>` and `git push origin --delete <branch>`; then `git fetch origin --prune`. Never force-delete unmerged branches or another agent's work. Cleanup otherwise requires explicit abandonment and artifact recovery.
