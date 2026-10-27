# ADR-6: measure quality, per-camera capacity and overload explicitly

Date: 2026-10-19 · Reviewed: 2026-10-20 · Status: Accepted; E-1 baseline validated, capacity pending · Scope: all epics.

## Context and decision

Camera count has no useful meaning without workload and acceptance criteria. Keep input FPS, decoded FPS and analyzed FPS separate, with per-camera rates and model-pass counts. A 25 FPS input sampled at 4 FPS can still require decoding all frames. 100 cameras at 4 analysis FPS means 400 analyzed frames/second before extra model passes; it is a test workload, not a capacity prediction.

Every benchmark manifest records host/driver/runtime/container versions, GPU UUIDs and competing workloads, input checksum and codec/resolution/FPS/bitrate/GOP, replay pacing, model/config/profile hashes, precision/batch/image size, tracker/rule settings, warm-up and measurement duration, rendering/evidence settings, queue policy and clocks. Measure synchronized GPU inference timing separately from pipeline wall-clock processing. Exclude load/warm-up from steady-state summaries and report them separately.

E-1 offline maximum-speed throughput is not live event latency. For live/realtime-paced tests record source-to-event latency when clocks support it, and separately ingest-to-event latency when capture time is unknown. Primary rule latency starts when the rule becomes eligible (after the dwell threshold, for example). Secondary verification latency is measured separately from the original primary event.

## Protocol and gates

1. Fix workload and quality rubric; capture a small annotated positive/negative set and held-out samples for later tuning.
2. Warm up, then run increasing camera points under identical settings, recording per-camera FPS, event latency p50/p95, queue depth/oldest age, drops by reason, compute/decoder utilization, CPU/RAM/VRAM, and storage/network load. If a metric is unavailable, label it unavailable rather than substituting an unrelated number.
3. A reported stable capacity point requires ≥4 analyzed FPS per camera, primary-event p95 <3 seconds, no sustained queue growth and no worker crash for at least 30 minutes. State configured buffer limits, drop tolerance and event-quality tolerance before running. Fairness matters; aggregate FPS alone cannot pass.
4. Report misses/false alerts and event matching tolerances. Include failure clips and sample counts; unagreed production quality thresholds make production acceptance provisional. Compare correctness before/after export, precision changes, tracking migration and higher load.
5. Record the first failing point, bottleneck and tradeoffs. Repeat only after a meaningful change or to resolve uncertainty. Use longer soaks for pilot readiness once duration is agreed in B-009.

## Alternatives, consequences and revisit

Vendor channel counts or inference-only FPS cannot certify this system. The protocol costs GPU time and annotation work but prevents optimistic scaling claims. Keep raw metric samples and reports under `benchmarks/e-N/`, large artifacts outside Git. Revisit targets with real stream parameters, event semantics, accuracy requirements or hardware changes; preserve original results with their manifest.

E-1 supplied a reproducible single-stream processing baseline. E-2 supplied
functional event counts but no labelled precision/recall set, so it is not a
quality gate. No concurrent-camera, live-latency or 30-minute capacity point has
been measured; all such capacity claims remain pending E-3.
