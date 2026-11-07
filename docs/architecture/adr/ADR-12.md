# ADR-12: isolate agent work and require human plan and commit gates

Date: 2026-10-20 · Revised: 2026-11-07 · Status: Accepted by owner direction · Scope: development workflow.

## Context

AVA is developed by different coding clients and local models, including Copilot and Qwen/Gemma routes with bounded context. Parallel agents sharing one checkout can overwrite unrelated edits. Context clearing or compaction can also lose decisions that exist only in conversation. Private `docs/`, footage, weights and results are ignored and do not appear automatically in worktrees. Automatic agent commits also bypass human review, while implementation started from an under-specified plan can encode assumptions the owner never accepted.

## Decision

`AGENTS.md` is the single authoritative source for operational agent workflow and human gates. The owner chose combined human QA and plan approval before development, followed by exact-change human approval before task commits or history rewrites. A confirmed plan may authorize integration through a mergeable PR with passing checks without a separate routine merge gate. Worktree isolation, durable checkpoints and preservation of private assets remain required as specified in `AGENTS.md`.

`docs/intro.md` is the private session entry point and handoff, and links to `AGENTS.md` for current rules. Historical approvals and handoffs describe the rules in force at the time.

## Alternatives and consequences

A shared checkout is simpler but unsafe under parallel edits. A separate clone per agent duplicates objects and weakens shared branch visibility. Automatic context clearing after every response is client-specific and can discard an unfinished checkpoint; durable handoff followed by a fresh session is portable.

Worktrees isolate tracked edits but not semantic conflicts, shared services or manually shared ignored assets. Human QA and task-commit approval add latency but ensure requirements and changes receive review; an authorized integration path avoids a redundant routine merge gate. Short instructions reduce prompt overhead but require links to detailed plans, ADRs and progress evidence.

## Validation and revisit

Validate with recorded human plan confirmations, approval evidence for each task commit, `git worktree list`, unique active branches, clean task handoffs and combined tests after integration. Revisit if the repository adopts a central orchestration service, remote ephemeral workers or versioned private documentation that changes asset and handoff ownership. Automation may execute the confirmed integration path but must not approve the QA or task-commit gates on the human's behalf.
