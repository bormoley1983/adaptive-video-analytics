# ADR-10: private documentation and manual remote handoff

Date: 2026-10-20 · Status: Accepted by owner direction, amended 2026-10-27 · Scope: repository and all sessions.

## Context and decision

Development/design documentation is private for now. Keep the root `docs/` tree out of Git using `/docs/*` in `.gitignore`. This includes source ideas, epics, design, backlog and daily progress. The owner will transfer it manually to the remote AI-host session after initial planning review. Exclude it from Docker build contexts/images using `.dockerignore` as well.

**Amendment 2026-10-27 (owner-approved):** two stable policy documents are committed to Git as public exceptions: `docs/tests_rules.md` and `docs/architecture/` (ADR index, ADRs and PlantUML diagrams). Rationale: they are referenced by the public README, AGENTS.md and benchmark reports, contain no planning detail or client material, and change only when policy or architecture decisions change. The `.gitignore` rule is therefore `/docs/*` plus explicit negations for these two paths; the ignore rule must not be widened back to `/docs/` without removing the negations first. Everything else under `docs/` remains private and manually transferred.

At the time of this decision, `git ls-files` returned no tracked files, so no index removal or history rewrite is needed. Ignore rules do not remove already committed files if the policy changes later. Do not force-add private docs or copy them into code, image layers, public PR text or release artifacts. Runtime schemas, sanitized example configs, application README/run instructions and code remain eligible for normal version control; private planning is not needed to run a shipped container.

## Manual handoff procedure

1. Finish the current review and update `docs/intro.md`, the ADR index, backlog and latest daily progress record.
2. Give the private handoff a date/revision identifier in its daily log; record the code commit separately when one exists. Preserve relative `docs/` paths and optionally file checksums when packaging.
3. The owner copies the full private docs tree to the remote checkout/workspace, preserving the same paths. Code clone/checkout alone will not contain these files.
4. In the remote session, read `docs/intro.md` first, confirm the transferred revision and record the tested code commit, container digest and fresh host manifest. Verify ignore rules before staging code.
5. Continue updating the private daily logs. The owner manages subsequent manual synchronization and backups; Git does not provide documentation history or recovery for ignored files.

Before the first code commit, verify `git ls-files docs` is empty and `git check-ignore` matches representative private files. Review staged paths with `git diff --cached --name-only`. Public Git status/diff cannot review changes inside ignored documentation; inspect those files directly.

## Alternatives and consequences

A separate private documentation repository could provide history later, but is not the current workflow. Selective exceptions inside `docs/` could accidentally expose planning details; the 2026-10-27 amendment accepts that risk for two explicitly named stable policy paths only, and any further promotion requires a new owner decision recorded here. Ignoring the complete tree is simple and follows the owner's current sharing boundary; manual transfer/versioning/backups become essential.

## Validation and revisit

Validate ignore coverage and link integrity after edits. Revisit only when the owner changes the publication/synchronization policy. No remote transfer, Git commit, public publication or infrastructure connection is performed by this documentation revision.
