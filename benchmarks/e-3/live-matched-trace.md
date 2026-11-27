# Matched live trace — four-point attribution (D4)

Measured 2026-11-21. All four points completed. The control and live runs recorded similar sampled decode-read durations. **Bottleneck attribution is inconclusive.**

## Configuration and measurement boundaries

One RTX 3090 runs YOLO26n on fifty 1280×720 replay streams at 10 source frames per second. The maximum batch size is 16. The common windows are 300 seconds except for the 60-second smoke. Timing summaries include selected calls completed inside those windows. The frozen manifest matched the D3 reference for all 58 non-cache files, including the control helper, and for all 51 inputs (50 clips and the model). The checkpoint records a CPU-only reproduction that loaded the frozen package through both recorded Python entry points and matched all 58 frozen files to the run manifest. The underlying probe log is unavailable, so that reproduction could not be independently verified during closeout.

Owned publishers were reset before each RTSP-consuming case. The exact source frame phase remains uncertain.

## Four-point sequence

| Case | Mode | GPU | Window | Observed result | Boundary proved |
| --- | --- | --- | --- | --- | --- |
| d4-smoke-4 | combined | yes | 60 s | 10 FPS per camera | Small-scale profiled pipeline and ledger check |
| d4-decode-50 | decode | no (CPU) | 300 s | 500.0 aggregate decoded FPS | RTSP read and decode supply under this replay setup |
| d4-backend-16 | backend | yes | 300 s | 513.227 completed FPS, batch 16 | Static first-sixteen-image input distribution |
| d4-trace-50 | combined | yes | 300 s | 6.673–6.677 analyzed FPS per camera | Full live pipeline with device profiling |

All four cases exited 0, the combined run ledgers reconcile, and owned resources were removed. In trace-50, 110,944 inferred frames were counted across all phases. The measured cohort delivered 100,116 frames.

## Matched-trace validation against D3

Trace-50 and the D3 combined on point used matched inputs, source files and measurement windows, with case-specific RTSP URLs. The major stages below differ by about 8% or less. A few small sink stages differ by more. The comparison consists of one pair.

| Stage (sum of selected spans) | d4-trace-50, s | d3 combined-on, s | ratio |
| --- | ---: | ---: | ---: |
| decode_read_host_wall | 937.931 | 937.367 | 1.00 |
| enqueue_to_submit_host_wall | 309.635 | 288.031 | 1.08 |
| offer_to_submit_host_wall | 303.799 | 283.032 | 1.07 |
| tracking_host_wall | 58.230 | 55.656 | 1.05 |
| executor_call_host_wall | 17.769 | 17.806 | 1.00 |
| prediction_host_wall | 17.558 | 17.603 | 1.00 |
| delivery_queue_wait_host_wall | 14.811 | 14.238 | 1.04 |
| acknowledgement_wait_host_wall | 9.087 | 8.423 | 1.08 |
| predictor_preprocess_host_wall | 6.510 | 6.992 | 0.93 |
| predictor_postprocess_host_wall | 2.090 | 2.030 | 1.03 |
| predictor_inference_host_wall | 1.453 | 1.411 | 1.03 |

## Attribution and next measurement

The selected decode-read spans total 937.931 seconds in trace-50 and about 936.6 seconds in the CPU decode control. The D3 trace value is 937.367 seconds, a difference of 0.06% from D4. Trace-50 sums 9,374 selected decode reads across 50 concurrent cameras and 391 selected prediction batches. Concurrent and nested spans overlap, and the stage totals use different sample units. Similar decode-read totals across runs cannot establish how much decode and prediction overlapped or which limited throughput.

Trace-50 recorded 391 device samples across 6,934 batch calls with no truncation. The inference-labelled interval was 42.9 ms at p50 and 48.1 ms at p95. Both device intervals cover the whole prediction call on the declared stream. Isolated transfer and kernel times, selected-batch observer perturbation, and paired same-clock host residuals are unavailable for this attribution.

The next narrow measurement is to compare paced RTSP reads with decode of the same encoded inputs without RTSP pacing, keeping decoder settings and input mix fixed. Record read and decode boundaries on the same clock, and test whether the observed difference changes live throughput before assigning a bottleneck. Trace-50 produced 241 unique events, with 216 measured event-latency samples across 12 cameras. The other 38 cameras had no latency samples. These results make no claim about event quality or stable capacity.

## Backlog for live throughput attribution

The RTSP pacing comparison above covers ingest. These follow-ups cover batch scheduling and the prediction path.

1. Inspect the saved trace for batch-size distribution and submission reasons. If the trace lacks those fields, record them in the next approved run. Use them to distinguish full batches from partial batches submitted on timeout.
2. If timeout submissions are common, compare the baseline with one alternative `max_wait_ms` value. Keep the replay and other settings fixed. Compare aggregate and per-camera analyzed FPS, candidate replacements, and queue wait. Set the acceptable latency bound before the run and report where latency samples are unavailable.
3. If batches are usually full or the scheduling change has little effect, run a matched pair that changes only detector workload. Record actual batch sizes, analyzed FPS, and candidate replacements. A throughput change can implicate the prediction path. To assess GPU compute specifically, collect device timing that separates kernels from transfers and host work before deciding whether a GPU upgrade would help.

See [the sampled attribution report](live-stage-attribution.md) and [README usage](../../README.md#opt-in-live-stage-measurements).
