# ADR-8: optional event-triggered advanced verification

Date: 2026-10-19 · Revised: 2026-10-22 · Status: Proposed pending E-5/E-7 value and feasibility results · Scope: E-5/E-6/E-7.

## Context and decision

Continuous detection/rules have a stronger throughput requirement than advanced
semantic reasoning. Keep the primary path independent. E-5 evaluates models AVA
might run itself, such as localization, segmentation/tracking and compact VLM
candidates. E-7 evaluates the already running local-AI service through LiteLLM.
No capability is adopted before a defined rubric verifies value, privacy,
compatibility, resource cost and quality on representative examples.

If a candidate adds value, send selected snapshots or bounded short clips through a secondary queue. Queue messages carry event/evidence references, not arbitrary raw-frame streams. Define maximum queue length, evidence bytes/clip duration, TTL, concurrency, retry budget and queue age. Deduplicate related incidents and expire stale jobs. When saturated, preserve the primary alert and publish deferred/expired/failed status plus metrics; do not block primary inference or silently imply confirmation.

Measure arrival and completion rates under the intended trigger policy. Sustained arrival above service capacity requires stricter triggering, more resources or accepted expiry, not an infinite queue. Treat verifier failures, ambiguous output and malicious/instruction-like text visible in footage as untrusted observations; model output cannot invoke tools or change configuration. Explanations cite evidence/times and expose uncertainty.

## Alternatives and consequences

Running a VLM on every camera/frame risks resource starvation without evidence
of value. Making verification mandatory for primary publication hides alerts
during outages. The asynchronous design produces two stages of incident state
and needs clear operator/API semantics. The default topology keeps video work on
the shared AVA GPU and calls the existing local-AI service on its reserved GPU.
Compare alternative placements only in an explicitly approved test window; do
not assume two GPUs are generally available or that specialization is best.

## Validation and revisit

E-5 supplies AVA-controlled model evidence; E-7 supplies shared-service
capability, quality/cost and failure-isolation evidence. E-6 validates only the
capabilities admitted to its pilot. If no candidate improves a required task,
leave it disabled. Revisit when semantic requirements, service APIs, response
deadlines or available GPUs change.
