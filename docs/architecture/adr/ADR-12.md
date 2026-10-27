# ADR-12: isolate agent work and require human plan and commit gates

Date: 2026-10-20 · Revised: 2026-10-22 · Status: Accepted by owner direction · Scope: development workflow.

## Context

AVA is developed by different coding clients and local models, including
Copilot and Qwen/Gemma routes with bounded context. Parallel agents sharing one
checkout can overwrite unrelated edits. Context clearing or compaction can also
lose decisions that exist only in conversation. Private `docs/`, footage,
weights and results are ignored and do not appear automatically in worktrees.
Automatic agent commits also bypass human review, while implementation started
from an under-specified plan can encode assumptions the owner never accepted.

## Decision

- Give each parallel task one sibling worktree, unique `agent/<task>` branch,
  explicit committed base, observable outcome and owned file set.
- Before implementation, require a human QA session to resolve every missing
  detail, decision, assumption and acceptance criterion. Record the answers,
  present the revised plan and wait for explicit human confirmation. Material
  scope or assumption changes repeat this gate.
- Agents prepare focused, reviewable changes. Before every commit-writing or
  commit-rewriting operation, a human reviews the exact diff, validation results
  and proposed message and explicitly approves that one commit. A material diff
  or message change invalidates the approval. This includes commit, amend,
  merge, squash and cherry-pick operations.
- Human approval is non-delegable and cannot be inferred from silence, the task
  request, prior approval, successful tests or another agent's approval. Without
  approval, work stops at the relevant gate with reviewable uncommitted changes.
- One integrator owns overlap resolution, combined validation and shared private
  progress/handoff updates, subject to the same human gates.
- Supply ignored `docs/`, `videos/` and `models/` read-only only when required;
  keep results worktree-local and never copy credentials between worktrees.
- Keep one bounded subtask per agent session. Before clearing, compacting or
  replacing context, record branch/base, changed files, behavior, exact checks,
  limitations, private-doc requests and the next action in a durable handoff.
- Use a fresh session for materially different work. Remove branches/worktrees
  only after integration or explicit abandonment and artifact preservation;
  never force-clean another agent's work.

`AGENTS.md` is the concise authoritative rule set. `docs/intro.md` contains the
owner/integrator commands and current handoff.

## Alternatives and consequences

A shared checkout is simpler but unsafe under parallel edits. A separate clone
per agent duplicates objects and weakens shared branch visibility. Automatic
context clearing after every response is client-specific and can discard an
unfinished checkpoint; durable handoff followed by a fresh session is portable.

Worktrees isolate tracked edits but not semantic conflicts, shared services or
manually shared ignored assets. Integration remains explicit and costs review
time. Human QA and per-commit approval add latency, but prevent agents from
silently choosing requirements or writing repository history without review.
Short instructions reduce prompt overhead but require links to detailed plans,
ADRs and progress evidence.

## Validation and revisit

Validate with recorded human plan confirmations, approval evidence for each
agent-created commit, `git worktree list`, unique active branches, clean task
handoffs and combined tests after integration. Revisit if the repository adopts
a central orchestration service, remote ephemeral workers or versioned private
documentation that changes asset and handoff ownership. Automation may make the
gates easier to audit, but must not approve on the human's behalf.
