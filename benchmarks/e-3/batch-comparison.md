# Corrected live inference: batch 8 and batch 16

Measured 2026-10-28. Both configurations sustained approximately 10 FPS per
camera for 50 live cameras during separate 60-second diagnostic windows.
Batch 8 is sufficient for this offered load and uses less device memory.
These short points do not establish sustained system capacity.

## Pipeline and measurement

The sources supply H.264 over TCP RTSP at nominal 10 FPS, 1280 × 720.
The corrected pipeline is depayloader → parser → NVDEC → one-buffer
oldest-frame-dropping queue → GPU conversion → stream mux → GPU square
preprocessing → inference → sink. Queue byte and time limits are disabled.
Moving bounded dropping after decoding preserves complete decoded frames and
removes the application's leaky queue before the depayloader. The RTSP jitter
buffer retains its configured latency/drop policy.

The mux retains source resolution. Preprocessing scales content to 640 × 360
and pads 140 rows above and below to produce a 640 × 640 RGB tensor, normalized
to FP32 NCHW. This preserves the source aspect ratio for the square model input.
Batch 8 and 16 use separately built FP16 engines with dynamic batch support.
The runner, queue policy, native telemetry, frame ledger and verifier are
unchanged between these two points; engines and adapter libraries are read-only.

Each point waits for all cameras to decode and infer, warms up for five seconds,
quiesces instrumentation, measures a common half-open 60-second window, then
sends EOS and drains. Readiness, drain and NULL shutdown have explicit bounds.
Image/tensor readback and verbose parser auditing occur before the timed window;
compact frame records, raw-frame accounting and resource sampling continue.
Frames are correlated by source, session and presentation timestamp.

## Results

| Metric | Batch 8 | Batch 16 |
|---|---:|---:|
| FPS per camera | 9.983–10.017 | 9.983–10.000 |
| In-window inference frames | 29987 | 29985 |
| Actual mean batch | 7.9987 | 15.4721 |
| GPU compute mean / peak | 29.73% / 33% | 28.40% / 36% |
| GPU decoder mean / peak | 47.78% / 50% | 48.03% / 50% |
| Device VRAM mean / peak | 3181.7 / 3434 MiB | 3745.8 / 3759 MiB |
| Runner CPU mean / peak | 91.99% / 98.82% | 89.68% / 101.52% |
| Runner RSS peak | 2.684 GiB | 2.838 GiB |
| All-stage reconciled admissions | 33261 | 33230 |
| Outstanding frames / invalid boxes | 0 / 0 | 0 / 0 |
| Measured decoded queue displacements | 0 | 0 |
| Sampled source boxes checked | 343 | 370 |

Configured batch size is a maximum: the mux can emit partial batches. Batch 8
recorded 3,748 full batches and one batch of three in the window; batch 16
recorded 1,577 full batches and 361 partial batches of size 6–15.

Runner CPU is measured for the actual inference process; 100% means one logical
core. Device VRAM includes the existing baseline and any other allocations; it
is not an exclusive process allocation. GPU compute and decoder utilization are
separate sampled metrics. Neither point saturated measured GPU compute.

A separate host sampler during batch 16 recorded 60 samples across 24 logical
CPUs: total busy averaged 7.96%, peaked at 30.21%, and mean iowait was 0.043%.
The busiest individual-core sample was 85.86%. These host totals include other
services. Batch 8 has no corresponding host-wide CPU capture; the noisy service's
individual consumption and equal host contention cannot be inferred.

## Verification and interpretation

Both independent functional and resource verifiers passed. All admissions
reconcile through mux, preprocessing and inference. Each measured admission
cohort has 15 completions during drain rather than inside the half-open window;
there is no warmup carry. Neither measured cohort has timestamp regressions or
gaps over 200 ms. Three prewindow tensor/mux samples per point exactly match
the CPU reference including padding, and releases complete. Sampled source boxes
match the inverse transform; all emitted boxes pass finite/bounds checks.

The decoded-frame ledger records 33,288 inputs/outputs for batch 8 and 33,272
for batch 16, with zero displaced frames or unforwarded tails in either run.
No forward RTP sequence gaps were observed at the instrumented pad in either
measured attempt. These are bounded observations, not a guarantee of universal
upstream losslessness. The preceding batch-8 smoke had one prewindow decoded
queue displacement and two RTP gap observations; it is distinct from this table.

Batch 16 uses about 564 MiB more mean device memory for the same offered
throughput, while its observed compute and process CPU means are slightly lower.
Each configuration has one measurement point. Source start phases and other
host activity are uncontrolled, and engine profiles differ. The comparison
does not establish statistical optimality or attribute all differences solely
to batch size. Earlier batch-16 runs used the old queue/instrumentation and are
excluded from this matched comparison. Earlier intermittent decoder stalls were
not reproduced here; the historical root cause remains unproven.

EOS, NULL, container removal and GPU baseline restoration passed. The existing
workload on the other GPU retained its process allocation. On this host the
owner-approved workaround exposes both physical device nodes while CUDA and
SDK plugins select the inference GPU; this is not physical GPU isolation.

## Evidence and acceptance boundary

The Make-driven run and separate functional/resource verification returned zero
for both points. CPU checks cover queue behavior, raw-frame accounting, frame
windows, failure checkpoints, tensor/coordinate verification and the added host
CPU sampler. The matched configuration/source checks and shell syntax passed.
Host Ruff was unavailable for the diagnostic checks; no lint pass is claimed.
No additional GPU or production test was run to prepare this document.

Raw logs, per-camera CSVs, configurations, sample tensors, resource snapshots,
independent JSON verdicts and hash manifests are retained privately outside Git.
This report publishes aggregate measurements only. It accompanies the existing
[compatibility and provisional capacity report](report.md), and does not replace
its historical results or the [capacity correction ADR](../../docs/architecture/adr/ADR-17.md).

The short inference diagnostic is complete. Color/accuracy, model lineage,
event latency, longer stability, sustained capacity and production integration
remain unaccepted. The diagnostic runner and adapter are not promoted into the
tracked runtime by this documentation change.
