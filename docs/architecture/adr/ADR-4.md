# ADR-4: shared rule semantics, explicit clocks and configuration

Date: 2026-10-19 · Reviewed: 2026-10-20 · Status: Accepted; partially implemented · Scope: E-2/E-3/E-4.

## Context and decision

Use validated YAML/JSON camera, model and rule profiles with schema/config versions. Store geometry in normalized source-image coordinates and convert to the actual processing dimensions. Keep object-class prompts independent of line/polygon/time conditions. Invalid configs fail before processing; E-2 applies changes on rerun/restart. Hot reload is a later requirement, not an implied feature.

Use the bounding-box bottom-center anchor consistently for person/vehicle geometry. Crossing requires a tracked segment crossing the configured finite line with direction and a configurable deadband/debounce. Polygon entry is an outside→inside transition; a track first seen inside is occupancy and starts dwell but does not imply observed entry. Count accepted crossings per direction. Dwell is continuous presence measured in source seconds, reset on confirmed exit or expiry of a configured missing-track grace interval. Do not infer continuity across reconnects or tracker identity resets.

Frame contracts retain source PTS/frame index, camera/session identity, ingest time and processing timing. Use source PTS for recorded-video rules and monotonic timers for durations. Keep UTC event timestamps separate from processing clocks; derive replay UTC only from an explicit replay origin and label it synthetic. Record source-clock uncertainty for live latency. Discontinuity starts a new session and resets rule state. Track identity is `(camera_id, session_id, track_id)`, not a globally recognized person.

## Alternatives and consequences

Free-form natural language for all rules would obscure testable semantics. Frame-count timers break when sampling rates change. Backend-native rule output alone can silently change behavior during migration. Use `nvdsanalytics` for supported spatial work and explicit custom state for dwell/debounce as needed, with a config translator and parity fixtures. NVIDIA's [analytics plugin documentation](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvdsanalytics.html) describes ROI, direction and crossing features and its bottom-center anchor (checked 2026-10-19).

## Validation and revisit

E-2 deterministic fixtures cover direction, finite lines, jitter, sparse/missing
frames, re-entry, grace expiry, first observation inside and clock resets. The
real clip demonstrated line and zone events. Full conformance is not complete:
ingest derives time from frame index/FPS rather than source PTS, discontinuity
does not allocate a new session identity, and polygon validation does not reject
self-intersection or zero area (B-013/B-014).

E-3 must replay the same cases and report semantic/tracker differences. Revisit
for unattended-object association, calibration, cross-camera identity or hot
reload; record new semantics before implementation.
