# Sampled live pipeline attribution

Measured 2026-11-08. The optional observer is validated, and its sampled configuration meets the observed throughput-overhead budget. Decode supply passes this workload; the live pipeline still fails the five-minute optimization target. The internal CPU/GPU cause remains unresolved. These results certify neither inference quality nor stable capacity.

## Configuration and measurement boundaries

One RTX 3090 runs YOLO26n with IEEE FP32, confidence 0.35, IoU 0.5, image size 640 and rect=False. Inputs are fifty 1280×720, 10-FPS replay identities from four distinct normalized clip contents; tracking/rules remain camera-local. Maximum batch 16 and a 20 ms deadline, all-camera/model readiness, 30 s warm-up and 300 s common windows are fixed. The sampled four-camera smoke uses 60 s. Diagnostics explicitly use JSONL; PostgreSQL remains the production default and was not capacity-benchmarked here.

Runtime: Python 3.12.3, torch 2.14.1+cu130/CUDA 13.0, OpenCV 5.0.0, Ultralytics 8.4.183; observed driver 595.99.02. The immutable cached runtime image is `sha256:5387c1f90e9be1087f1df9053e4b3448a5411cd72c6568586d8b098e3e66d5a6`, with the frozen candidate source mounted read-only. All 57 production source files, model and replay inputs match across the accepted controls/pair/smoke. The isolated control helper has its separately recorded version. Full commands, failed attempts, source/input manifests, logs and independent audits remain private.

Replay servers/publishers and worker containers are uniquely owned, use an isolated replay network/dynamic loopback port, and mount existing clips/model read-only. Only owned publishers are reset before the off/on pair. Original guards require 16 GiB free on the selected GPU before a point and stop owned work below 4 GiB available host RAM; workers have an outer deadline. Other GPU allocation and shared services are preserved by command scope.

## Results

| Control | Window | Observed result | Boundary proved |
| --- | --- | --- | --- |
| Four-camera sampled smoke | 60 s | 10 FPS each, zero measured loss|Basic profiled pipeline and exact ledgers|
| Decode-only,50 cameras | 300 s | 10 FPS each |Decoded supply under this observer/source configuration|
| Repeated fixed-input backend, batch 16 | 300 s | 515.733 FPS |That static first-sixteen-image distribution only|
| Live,profiling disabled | 300 s |336.053 aggregate FPS;6.720–6.723 per camera|Full pipeline,cohort loss32.77–32.83%|
| Live,sampled profiling enabled | 300 s |330.880 aggregate FPS;6.617–6.620 per camera|Full pipeline,cohort loss33.80–33.87%|

The observed off/on penalty is 1.5394%, within the declared 5% budget. This is one sequential pair with publisher resets, not bit-identical replay or a statistical overhead guarantee. A prior fully collected observer incurred 7.9631% and was rejected; deterministic stage sampling and less frequent native-thread CPU collection reduced the observed penalty. A separate owned-publisher bulk reset exceeded its initial 30 s CLI deadline before its paired runs started; its failure and cleanup are retained. The corrected bulk deadline is bounded for 50 publishers.

Accepted live and smoke full/cohort terminal ledgers reconcile, arithmetic settings restore, no worker remains unresolved, and owned resources are removed. Both live points fail the every-camera 9.5 FPS/≤1% optimization gate. Eventless cameras retain unavailable latency; observed-event latency cannot pass a whole-camera or quality gate.

## Host timings and what they do not attribute

| Selected host-wall span | Fixed-input backend mean,ms | Live sampled mean,ms |
| --- | ---: | ---: |
| Outer prediction | 30.708 | 44.656 |
| Predictor preprocessing | 9.104 | 19.957 |
| Predictor inference submission | 2.773 | 3.797 |
| Predictor postprocessing | 4.158 | 5.467 |
| Result conversion | 0.321 | 0.509 |

These are selected host-wall spans,not isolated CPU execution or device kernel times. Preprocessing can include transfers; inference submission can return before GPU completion. The predictor-method means do not account for the full outer prediction interval: framework/synchronization work remains unattributed. Concurrent/nested spans must not be summed as an additive critical path. Different dynamic images, batch composition and contention also limit comparisons with repeated static inputs.

Every fully contained sampled live batch has16frames. Summed whole-backend call durations occupy approximately 93.5% of the 300 s window,using a completion-time placement proxy; receive timestamps can differ from executor completion. This identifies prediction as the dominant measured serialized boundary,not its internal causal mechanism. Device utilization or host CPU load alone cannot choose a correction.

A separate three-batch fixed-input CUDA trace contains 3,540 kernel events with summed duration 0.056267446 s and separately identified transfer events. Duration sums can overlap and are not live critical-path time or a representative dynamic 50-camera backend ceiling. The trace is outside the throughput measurement window.

## Observer contract

Enable `live.batch.profile_stages: true`; `live.batch.profile_sample_every` is a strict positive integer, default 16. Frequent spans select every sixteenth call per stage/camera; sparse persistence spans select every call. Counts/totals describe selected spans entirely inside the measurement window. `calls_all_phases` includes warm-up/drain and is not a window denominator. Frame,event and terminal-ledger counters are not sampled.

Each stage retains at most 1,024 recent selected durations. Percentiles are lower empirical quantiles of those retained samples; counts,truncation and sampling intervals are explicit. They are not unsampled whole-window tail guarantees. Full native-thread CPU sampling occurs no more often than every 15 s, with actual interval endpoints; unscheduled/invalid intervals remain unavailable. Process/thread CPU uses 100% per core, host CPU uses the whole host,and generator CPU is reported separately. Raw per-span timestamps are not exported; source logic/regressions validate filtering,but independent per-span window reconstruction is not claimed.

## Environmental observations and next boundary

Strict shared-container state invariance did not hold across every accepted run: two pre-existing publishers changed from restarting to running, and pre-existing Keycloak changed health/state during smoke. Causes remain unestablished. All pre-existing container IDs remain,recorded mutations target no pre-existing names/IDs,and owned cleanup/GPU process restoration pass. Preserve these as environmental observations rather than an unchanged-environment claim.

The next measurement must separate preprocessing, transfers, device execution and surrounding synchronization on matched dynamic inputs before selecting an optimization. Labelled event quality remains open. Neither provisional ≥30-minute throughput capacity nor quality-qualified capacity was measured; PostgreSQL capacity comparison is also unproven. See [the original batching report](python-batched-runtime.md) for the prior runtime contract and failed optimization matrix,and [README usage](../../README.md#opt-in-live-stage-measurements) for the feature controls.
