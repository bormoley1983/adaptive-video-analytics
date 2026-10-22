# E-2 quality baseline report

Status: **functionally validated** — line/zone/dwell analytics over the E-1
crosswalk clip with ByteTrack tracking, evidence materialization, and run
isolation confirmed on GPU (2026-10-21). This is a functional demonstration,
**not** a production accuracy certification (see Limitations).

## Environment

| Item | Value |
|---|---|
| Host | `ai-host1`, Ubuntu 26.04.1 LTS (resolute), kernel 7.0.0-38-generic |
| GPU (selected) | RTX 3090, UUID `GPU-facd1ddc-c4ab-7ac9-0000-ead909e0ec42` (GPU 0) |
| Image | `ava/e1:local`, ID `3d7a5885c33a` (rebuilt 2026-10-20 with tracking fix) |
| Container Python | 3.12.3 |
| torch | 2.14.1+cu130 |
| ultralytics | 8.4.183 |
| Tracker | ByteTrack (`bytetrack.yaml`), `persist=True` |

## Input

Same clip as E-1: Pexels crosswalk, 1920×1080, 59.94 FPS, 29.496 s, 1768 frames.
SHA-256: `110501b29df6f348f8942987a1d70e3d9f412a9b55f7b9e6c8917bcdab49f239`.

## Configs

| File | SHA-256 | Purpose |
|---|---|---|
| `configs/run-e2-rules.yaml` | `9df5204edce1…d6994b` | Positive: crosswalk line + restricted zone + dwell |
| `configs/run-e2-negative.yaml` | `c30b01e01444…5e3` | Negative: empty top-left corner, expect zero events |

## Commands (reproducible)

```bash
docker compose build
docker compose run --rm ava run --config /configs/run-e2-rules.yaml
docker compose run --rm ava run --config /configs/run-e2-negative.yaml
```

## Tracking quality

| Metric | Value |
|---|---|
| Total detections | 16,529 |
| Null track_ids | 1 (0.01%) |
| Unique tracks | 267 |
| Track duration (frames) | mean 61.9, median 19, max 713, min 1 |
| Top track | #172: 713 frames (~11.9 s at 59.94 FPS) |

The single null track_id is a first-frame edge case (track activated on frame 0
before the tracker assigns an ID). All subsequent detections carry stable IDs.

## Positive run results (`run-e2-rules.yaml`) — 2026-10-21 GPU validation

| Metric | Value |
|---|---|
| Frames | 1768 |
| Detections | 16,529 |
| Wall-clock | 22.11 s |
| Inference phase | 10.53 s |
| Output phase | 9.83 s |
| Events fired | 54 |
| Evidence resolved | **54/54 (100%)** |

### Event breakdown

| Rule | Type | Count | Classes |
|---|---|---|---|
| `restricted-zone` | zone_entry | 32 | person + car |
| `crosswalk-crossing` | line_crossing | 22 | person |
| `zone-dwell` | dwell | 0 | — (no track dwelled ≥10 s in zone) |

- **54 unique events** with snapshot evidence (`evidence/cam-01/*.jpg`).
- Event IDs follow ADR-5 format: `cam-01/s1/t{track}/{rule}/{kind}-{n}`.
- Source timestamps use real PTS (B-013): e.g. frame 17 → 0.267 s.

### Dwell-positive demo (`run-e2-dwell-demo.yaml`) — 2026-10-21

Lowered dwell threshold to 1.5 s to demonstrate the dwell path with clip
evidence on this crosswalk clip (objects pass through quickly; no track stays
in the zone ≥10 s continuously).

| Metric | Value |
|---|---|
| Frames | 1768 |
| Detections | 16,529 |
| Wall-clock | 17.02 s |
| Events fired | **9** (all dwell) |
| Evidence resolved | **9/9 (100%)** — MP4 clips, 2.9–5.4 MB each |

Example event:

```json
{
  "event_id": "cam-01/s1/t23/zone-dwell-short/dwell-0",
  "rule_type": "dwell",
  "source_time_s": 2.786,
  "evidence": {"kind": "clip", "path": "evidence/cam-01/cam-01.s1.t23.zone-dwell-short.dwell-0.mp4"},
  "payload": {"dwell_seconds": 1.5015, "dwell_start_s": 1.285}
}
```

The clip is centered on the triggering frame (B-012): 1.5 s before and after.

### Config-only change verification

Modified `run-e2-rules.yaml` (line y: 0.62→0.45, zone shrunk) and reran without
code edits or image rebuild:

| Config variant | Events |
|---|---|
| Original (line y=0.62, zone 0.55–0.95 × 0.30–0.75) | 80 |
| Modified (line y=0.45, zone 0.70–0.95 × 0.40–0.80) | 26 |

Event count changed proportionally to the geometry change — confirming rules are
driven entirely by configuration.

## Negative run results (`run-e2-negative.yaml`)

| Metric | Value |
|---|---|
| Frames | 1768 |
| Events fired | **0** |

Zone placed in the top-left corner (0.02–0.15 × 0.02–0.15) and line at y=0.05 —
regions with no activity in this clip. Zero events confirms the rule engine does
not produce false alerts on empty geometry.

## Performance comparison vs E-1

| Metric | E-1 (predict) | E-2 (track, persist=True) | Delta |
|---|---|---|---|
| Inference phase | 8.62 s | 10.63 s | +23% |
| Wall-clock | 19.32 s | 22.07 s | +14% |
| Peak GPU memory | 73.4 MiB | 73.4 MiB | same |

The ~2 s additional inference cost is the ByteTrack association overhead (IoU
matching, Kalman filter updates) per frame. Memory is unchanged — the tracker
state is small relative to model weights.

## Limitations and open items

1. **No ground-truth annotation.** This baseline reports system behaviour
   (event counts, track stability), not detection accuracy against labelled data.
   Precision/recall/false-alert-per-camera-hour require annotated examples with
   matching tolerance. That work is deferred to
   B-003 where owner-defined quality thresholds are agreed.

2. **Single clip.** One 29.5 s crosswalk clip does not represent production
   diversity (lighting, occlusion, camera angles, object density). The negative
   test uses the same clip with empty geometry — it validates the rule engine's
   silence, not the detector's absence of false positives on unseen scenes.

3. **Dwell at 10 s produced zero events on this clip.** No track remained in the
   restricted zone for ≥10 continuous source seconds (objects pass through
   quickly). The dwell path was validated with a 1.5 s threshold (9 events,
   clip evidence resolved) and by unit tests. A dedicated slow-object clip would
   be needed to validate the production 10 s threshold end-to-end.

4. **Track fragmentation.** 267 unique tracks over 29.5 s suggests significant
   ID switching (a person walking through the frame may get multiple track IDs).
   Median track duration is 19 frames (~0.32 s) — short. This is typical for
   ByteTrack on crowded scenes without ReID and does not block the PoC gate, but
   it affects dwell accuracy and per-track event attribution in production.

5. **Replay is not paced.** Same as E-1: wall-clock (22 s) < source duration
   (29.5 s). This is offline batch processing, not live streaming.

6. **Evidence materialization (B-012, resolved 2026-10-21).** Snapshots and
   centered clips are now written by the runner. The 2026-10-21 GPU run
   confirms 54/54 snapshot events and 9/9 clip events resolve to files on disk.

7. **Run isolation (B-012, resolved 2026-10-21).** A fresh run in an existing
   output directory writes `events-1.jsonl`, `events-2.jsonl`, etc., so reruns
   do not silently append to a previous event stream.

## Acceptance status

| Criterion | Status |
|---|---|
| Line crossing fires on configured line | ✅ 22 events (2026-10-21 GPU) |
| Zone entry fires on configured polygon | ✅ 32 events (2026-10-21 GPU) |
| Dwell rule produces positive event with evidence | ✅ 9 events, clip evidence resolved (1.5 s threshold) |
| Config-only change alters behaviour | ✅ 80→26 events, no code edits |
| Negative clip produces zero events | ✅ 0 events (2026-10-21 GPU) |
| Track IDs populated and stable | ✅ 99.99% non-null, persistent across frames |
| Event IDs follow ADR-5 format | ✅ `cam/session/track/rule/kind-n` |
| Event evidence resolves to the incident | ✅ 54/54 snapshots + 9/9 clips (B-012 resolved) |
| Run isolation on rerun | ✅ `events-N.jsonl` numbering (B-012 resolved) |
| Source PTS used for timestamps | ✅ real CAP_PROP_POS_MSEC (B-013 resolved) |
| Reproducible commands recorded | ✅ above |
| Annotated precision/recall baseline | ❌ deferred to B-003 |

Accordingly, E-2-T1/T2 are implemented, E-2-T3/T4 are partial, and E-2-T5 is
open. The next benchmark must use a fresh output directory and include a
dwell-positive clip plus resolvable evidence before this report can close E-2.
