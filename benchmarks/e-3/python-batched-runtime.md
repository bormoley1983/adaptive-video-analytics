# Python single-model live batching

The conventional Python/Ultralytics live runtime now separates bounded decode
queues, fair scheduling, one detector owner and camera-local ByteTrack/rules.
Numerical parity and the four-camera smoke pass. All three five-minute,
50-camera points fail the throughput/drop targets. No stable capacity is
certified, and the remaining cost is not yet attributed to decode, copies,
scheduling or GPU work.

## Runtime contract

Each camera has a bounded drop-oldest ingress queue, at most one candidate and
one in-flight frame. The size-or-deadline scheduler defaults to 16 frames/20 ms.
One executor thread loads, warms, invokes and closes one detection model;
per-camera processors own tracking and rules. Acknowledgement releases a camera
only after processing and event persistence. Session changes fence stale work.

PostgreSQL remains the default and persistence failure stops the run. Diagnostic
examples explicitly select JSONL. Shared/per-camera modes remain available.
Prompted batching is rejected before output/persistence startup. Selecting the
DeepStream backend raises an explicit unavailable-adapter error without fallback.

GPU arithmetic is IEEE FP32 for CUDA matmul and cuDNN convolution. The owner
checks policy around inference and restores previous settings during cleanup.
Logical shutdown cannot terminate a blocked native call. Unresolved work fails
the run and requires process discard; experiments also use an outer deadline.

## Reproducible profile

Measured 2026-10-29 on one RTX 3090, with the other GPU's workload preserved.
Pinned runtime: Ultralytics 8.4.183, torch 2.14.1+cu130, OpenCV 5.0.0;
YOLO26n, confidence .35, IoU .5, imgsz 640, rect=False, source BGR input.
The 50 loopback replay sources offer 1280x720 at 10 FPS each. Diagnostic configs
use container paths `/models/yolo26n.pt` and `/results/run` and explicit JSONL.

Concurrent Python connection requests do not disable native OpenCV locking.
These isolated diagnostic containers explicitly set the process-start option
`OPENCV_FFMPEG_IS_THREAD_SAFE=1`. Its compiled presence was checked in this image;
this is not a safety claim for arbitrary OpenCV/FFmpeg builds or a global default.
The fresh CPU-only control readied all 50 sources by 5.466 s, decoded at least
60 frames per source over the hold and finished without unresolved threads.

The GPU matrix uses identical production-source/model hashes. Every point has
all-source/model readiness, a 30-second warm-up and one 300-second common window.
The maximum batch sizes are 8, 16 and 32, with the same 20 ms deadline. Fresh
GPU/source/port/RAM preflight precedes each point. A separate supervisor records
host RAM and stops only its own container below 4 GiB; the outer deadline is
750 seconds. Full private logs, frozen source, commands, hashes, raw metrics and
independent ledger/resource audits are retained on persistent storage.

## Results

Fresh same-image detector parity uses 32 identical decoded images for each of
batch sizes 4/8/16/32: 128/128 comparisons pass exact class/count, box IoU >=.99
and confidence difference <=.001. Independently audited minimum IoU is
.9999907512 and maximum confidence difference .00000214577. The real ByteTrack
synthetic trace produces two deterministic positive events and zero negative
events with identical repeated identities. This does not establish footage
quality against annotations.

The four-camera 60-second smoke sustains 10 FPS each, measured loss 0–.167%,
sampled ingress depth zero and exact full/cohort ledgers. Observed event p95 is
28.5 ms; three cameras have no events, so latency is unavailable. Its inference
histogram initially recorded frame age and is excluded. A subsequent tested
telemetry correction reports executor-call duration; all matrix points use that
same corrected source. Detector/precision/tracking behavior is unchanged.

| Maximum batch | Actual mean batch | Per-camera analyzed FPS | Measured loss | Worst observed event p95 | GPU compute mean | Device VRAM mean |
|---|---:|---:|---:|---:|---:|---:|
| 8 | 8.000 | 5.970–5.973 | 40.267–40.333% | 133.2 ms | 41.02% | 905 MiB |
| 16 | 16.000 | 6.403–6.407 | 35.933–35.967% | 161.6 ms | 42.03% | 1,125 MiB |
| 32 | 25.002 | 6.747–6.750 | 32.500–32.567% | 203.6 ms | 43.20% | 1,667 MiB |

Each point fails the original every-camera >=9.5 FPS and <=1% measured-loss
thresholds. Sampled ingress depths are zero, but candidate replacement accounts
for most loss; bounded queues alone do not prove adequate throughput. Cutoff
cancellations count as losses. Full decoded and measurement-cohort terminal
ledgers reconcile exactly. FPS counts completions within the common window;
cohort delivery can also finish during drain, so those counts need not match.
Actual batches at maximum 32 alternate between 32 and 18 under the one-in-flight
camera constraint. A configured maximum is not the achieved batch size.

There are 196/196/203 measured event samples respectively; 38 cameras in each
point have no events and unavailable latency. Observed-event p95 is below three
seconds, without implying quality or latency acceptance for unobserved events.
All three fully recorded points exit zero, restore arithmetic and the GPU0
process baseline, remove their own containers and have no unresolved workers.

An earlier batch-16 attempt has a completed native report and reconciled ledgers,
but a chat interruption terminated its host supervisor at 125 seconds, losing
its exit/continuous-guard evidence. It remains explicitly partially verified.
The table uses one repeat under a detached supervisor, with unchanged source,
configuration and thresholds; the original attempt is preserved.

Fresh CPU gates against the final runtime: `make test-unit` 447 passed;
`make test-integration` 11 passed/11 PostgreSQL skipped; `make test-coverage`
92.87% (458 passed/11 skipped); `make lint` passed. `make test-e2e` collected
no tests (pytest exit 5/Make exit 2); `make test-infra` skipped all seven tests.
These unavailable suites are not passes; the manual packaged GPU runs above
supply their explicitly measured boundaries only.

## Limits and next gate

Host samples report load averages and available RAM, not CPU utilization or
causal attribution. Device VRAM includes baseline allocations. Workloads/source
phases are not statistically controlled, so these single points do not establish
an optimal batch size. Decode warnings were preserved; post-decode ledgers do
not prove absence of upstream loss or input-quality defects.

The previous temporary-worktree implementation and raw evidence were lost after
reboot. Historical chat measurements are not presented as verified artifacts;
this report concerns fresh reconstructed code and new persistent evidence.

Labelled footage quality remains open. Host/decode/copy/scheduling attribution
and optimization are follow-up work; at least 30 minutes and declared quality/
drop tolerance are required for stable-capacity acceptance. No further GPU run
is scheduled in this diagnostic matrix.
