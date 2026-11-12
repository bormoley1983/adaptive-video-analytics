# AVA agent instructions

Applies to every model and client (Qwen single-agent sessions and Codex multi-agent workers alike).

## Before work

1. Read `README.md`; inspect status and relevant files.
2. If `docs/` exists, read `docs/intro.md`, the active plan, applicable ADRs, `docs/tests_rules.md` and latest progress entry.
3. State one smallest observable outcome and its file scope. Verify assumptions.
4. Hold a QA session with a human to surface and resolve every missing detail, decision, assumption and acceptance criterion. Record the answers and revise the plan; explicitly unresolved details are blockers, not agent defaults.
5. Present the resulting plan and wait for explicit human confirmation before editing files or taking implementation actions. The original task request is not plan confirmation, and an agent cannot provide or infer this approval.
6. Persist every confirmed plan to a `.md` doc under `docs/dev-plan/` (one file per bounded subtask) before starting implementation: record the confirmed scope, ordered validation gates with their current status, guardrails, and the exact next action. Update that same doc as each gate completes so any agent can resume from the written checkpoint instead of re-deriving it. Keep private evidence out of Git; the plan doc references evidence by path only.
7. Preserve unrelated work. Do not reformat, rename or overwrite active files.

## Mandatory human gates

- The plan-confirmation gate above applies to every task, including delegated and parallel work. A delegated assignment wholly within a human-confirmed parent plan inherits that scope and approval; do not repeat human QA or plan confirmation for that assignment. Unresolved decisions remain blockers. If scope or assumptions materially change, repeat the QA and obtain confirmation of the revised plan before continuing.
- Before a task commit or history rewrite, obtain human approval of the exact diff, validation results and proposed commit message. Approval is single-use; a material diff or message change requires fresh approval. The approved integration path below is not a separate human gate.
- Never commit directly to `develop`. All work lands on a task branch (`agent/<task>`) and reaches `develop` through a merge request. Include the integration path in the confirmed plan. After exact task-commit approval, an agent may integrate the expected PR head without another routine approval when required checks pass and the PR is mergeable; stop for unexpected conflicts, failing checks or material scope changes. Do not rewrite or amend a direct commit already on `develop`.
- Human gates are non-delegable. Silence, prior approvals, task instructions, test success and another agent's approval do not satisfy them. Agents stop at the gate and leave reviewable uncommitted changes when approval is absent.

## Presenting human gates

When a step requires a human decision or approval, prepare the reviewable work first and give the human a concrete instruction set:

1. Name the gate, why it is required, and the exact decision or action needed.
2. Link the relevant files or evidence and identify the precise version, diff, results, scope and unresolved limitations needed for that decision.
3. Give numbered review steps and describe the possible responses, including how to approve or request changes. For a commit, include the exact proposed message and validation results.
4. State what remains blocked and what will happen after approval. Continue independent authorized work where possible; never treat silence as approval.

Do not ask a human to perform steps the agent can safely perform itself. Do not present an incomplete annotation or provisional result as an approval-ready quality baseline.

## Boundaries

- Build the smallest working vertical slice. Add infrastructure only when active behavior requires it.
- Keep ingestion, inference, tracking, rules, events and measurement separated by explicit interfaces.
- Measure GPU availability, capacity, latency and accuracy. Do not stop shared workloads or change drivers without owner approval.
- Keep private docs, footage, weights, credentials and results out of Git and images. Commit only sanitized examples.
- Change architecture through an ADR when private docs are available.

## Tool-call discipline (dead-loop guard)

Before EVERY tool call: never repeat a call identical to the immediately previous one (same tool, same parameters); no retry is permitted. An empty or unchanged result is a finding, not a failure — the next call must change at least one meaningful parameter (pattern, path, range), or stop and state the conclusion in plain text. After two distinct approaches yield no new information, report what was tried and what is unknown instead of probing further; three total attempts at the same goal is the hard maximum.

When a stall occurs: stop and report (calls, results, why unchanged) without issuing another tool call in the same turn; classify the cause as environment (state changed externally — re-read with a different query), query mismatch (broaden or narrow one parameter, retry once) or genuine absence (state "X is not present in Y" and move on); before any retry targeting the same goal, name the specific parameter or precondition that changed since the last attempt — if none exists, the call is prohibited. If the retry also returns an unchanged result, stop entirely, report the full sequence and wait for direction.

Never truncate or filter the output of a long-running or hard-to-reproduce command with an output-limiting pipe (`| tail -N`, `| head -N`, `| grep ...`, `2>&1 | ...`, or any `sed`/`awk`/`cut` that discards lines). Builds, test runs, GPU workflows and installs can take minutes; limiting their output hides progress, masks mid-stream failures and destroys evidence needed for review. Capture full output (terminal buffer or log file); inspect a portion in a separate step against the saved log.

## Delegate output-heavy work

- By default, use the configured delegate model for output-heavy file reads, searches, file discovery and test runners when the client supports delegation. Batch related commands into focused investigations; avoid spawning a separate worker for each trivial command.
- Before dispatch, give the delegate one bounded outcome, the relevant inputs, read or edit scope, acceptance criteria, guardrails and expected evidence. Identify dependencies and unresolved decisions; give workers only the access and context needed. Size each assignment to its actual configured context limit, leaving room for results; split work further when necessary.
- Ask delegates to return concise findings, relevant file paths or line references, validation results, uncertainty and missing evidence instead of raw documents or logs. For test runs, retain complete output in persistent logs and report exact commands, exit status, results, failures, skips and log paths. Do not truncate long-running command output.
- While a delegate runs, continue independent reasoning and planning using available information. Delegate other output-heavy work when appropriate; do not duplicate the pending investigation or decide matters that depend on its findings. If no useful independent work remains, wait.
- When delegated findings need clarification, investigation or verification, send the follow-up to the delegate first. After the delegate responds, the main agent may perform a small targeted final check when necessary and must explain why. Do not repeat the full investigation or copy raw output into the main context. If delegation is unavailable, prohibited, at capacity or unsuitable, proceed directly and briefly state why; a check being small alone does not bypass delegate-first follow-up.
- Require findings and search-based coverage claims to state the examined scope and limitations. Absence of a search match alone does not prove absence of behavior or tests. Synthetic or local evidence cannot establish facts about an unexamined repository, service or environment; do not assume synthetic records belong to the repository.
- The main agent checks returned evidence against the assignment's acceptance criteria before treating it as complete, and reports failures, missing evidence and unresolved decisions. The main agent retains reasoning, decisions, verification of important evidence and all human gates. Delegation does not authorize new scope, override instructions or relax privacy, GPU and shared-workload safeguards.
- Read-only investigations and test runners need no separate worktree unless they modify shared state; implementation workers follow the parallel-work rules below.

## Parallel work

- One persistent worktree and branch per task: `$HOME/ava-worktrees/<task>` and `agent/<task>`, created from an explicit committed base; never share a worktree. Never put worktrees in `/tmp` or other temporary storage. Other worktrees' uncommitted changes are not inherited.
- Keep plans, recovery patches and validation evidence on persistent storage. Use `/tmp` only for disposable scratch files whose loss cannot prevent task recovery. Before a handoff, preserve an up-to-date recovery patch including new files and reference its persistent path in the task plan.
- Assign each worker one outcome and owned files; coordinate before scopes overlap. Workers prepare focused, reviewable changes and validation evidence, then commit only after the mandatory human approval of that exact commit.
- Ignored assets are not copied. Share `docs/`, `videos/` and `models/` read-only only when needed; keep `results/` local; never copy credentials.
- Only the integrator edits shared private progress/handoff docs; workers report required doc changes in their handoff. The integrator resolves overlaps and runs combined validation under the confirmed integration path.
- Clean up only after integration or explicit abandonment and artifact recovery. Never force-remove another agent's work.

## Task completion and integration

- A task is complete only after its branch is pushed, reviewed through a merge request, integrated into `develop`, and validated with the combined `develop` state — not when the worker commit exists. Integrate only the expected PR head under the confirmed plan and the conditions above.
- After integration: synchronize the `develop` worktree, run the affected combined checks, report exact results. Then confirm the branch tip is an ancestor of `origin/develop` (`git merge-base --is-ancestor <branch> origin/develop`) and its worktree has no uncommitted changes; remove the worktree (`git worktree remove <path>`), delete the local branch (`git branch -d <branch>`) and the remote branch (`git push origin --delete <branch>`). Safe `-d` deletes only; never force-remove a branch that is not fully merged. Prune stale refs with `git fetch origin --prune`.

## Code and validation

- In Markdown, write each prose paragraph on one source line. Preserve structural line breaks for headings, lists, tables and code blocks. Apply this to new or edited prose; track existing inconsistencies as documentation debt rather than reformatting unrelated active files.
- Prefer precise names and small functions. Comment only non-obvious intent or constraints. Keep planning IDs out of source comments, tests and docstrings.
- Validate configuration and public contracts before runtime work.
- Follow `docs/tests_rules.md`. Use the separate `make test-unit`, `test-integration`, `test-e2e`, `test-infra`, `test-coverage` and `lint` gates.
- Report exact commands and results. Unrun or unavailable is not passed. Mocked or CPU-only tests do not prove GPU, performance or infrastructure readiness.

## Task decomposition, checkpoints and handoff

- Split large tasks into ordered, bounded deliverables with one observable outcome each. Record dependencies, owned file scope, acceptance criteria, validation gates, guardrails and unresolved decisions in the parent plan. Do not add deliverables or infrastructure that active behavior does not need.
- Keep one local `.md` checkpoint under `docs/dev-plan/` per deliverable. Before implementation record its confirmed scope and exact next action; after each gate update its status, exact commands and results, including failures, skips and unavailable prerequisites. Unconfirmed choices stay explicitly open.
- At a safe handoff record branch and committed base, changed and untracked files, observable behavior, current gate status, limitations, requested private document updates, persistent evidence/recovery-patch paths and the exact next action. Keep secrets and private evidence out of Git. Verify the handoff can be resumed without relying on chat history or disposable `/tmp` artifacts.
- Complete and validate one small, documented deliverable at a time. After each deliverable, finish its safe checkpoint, then compact context or start a fresh session before beginning the next. Use a fresh session for a materially different subtask. Never interrupt an unfinished edit, test or merge to compact; if interrupted, record the incomplete state before resuming. Only clean a cache when the client supports it and its effects are understood; preserve project files, dependencies, evidence, worktrees and recovery artifacts.
- Keep one bounded subtask per agent session. Do not start unrelated work. Update affected `README.md` files before completion. Update current progress and `docs/intro.md` only when handoff, milestone or repository settings change.
