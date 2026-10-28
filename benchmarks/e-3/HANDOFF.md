# Conventional DeepStream compatibility checkpoint

Date: 2026-10-24.
Branch: `agent/e3-t1-compat-spike`.
Base: `15c5c3829e5eb65963725f8e1b9b9e8d7f2d1635`.
Worktree: `/home/admaccess/ava-worktrees/e3-t1-compat-spike`.

## Start here in the next chat

Continue from this task worktree, not the older untracked parser/config files in
the develop checkout. Both approved hardware runs have already completed; do not
rerun them merely to resume. Read this file and `report.md`, then inspect status
and the uncommitted diff. The next step is review of this conventional slice;
YOLOE needs a separately scoped QA/plan session. Commit/push/MR/merge have not
occurred, and run approval is not commit or merge approval.

Private docs remain only in `/home/admaccess/adaptive-video-analytics/docs/`.
Read that checkout's `docs/intro.md`, `docs/dev-plan/E-3.md`,
`docs/tests_rules.md` and `docs/progress/2026-10-24.md` as required by AGENTS.md.
Do not assume ignored docs, videos or models were copied into this worktree.

The successful run's key evidence has been recovered from `/tmp` into the
Git-ignored directory:
`/home/admaccess/ava-worktrees/e3-t1-compat-spike/results/e3-t1-handoff-2026-10-24/`.
It contains detections, the full successful app log, successful decoder log,
both controlled capability-probe logs, parser build/test log, the exact runtime
parser library and all Makefile gate output. `SHA256SUMS` verifies all eight
artifacts; all checks returned OK. These artifacts stay outside Git and must be
recovered before removing the worktree.

## Approved scope and result

Human QA and plan confirmation authorized one conventional-model stream,
confidence 0.35, IoU 0.5 and a directional E-2 comparison without an accuracy-pass
threshold. YOLOE is a separate slice. The owner later explicitly approved both
GPU device nodes being exposed for a native capability query, then extended
approval to one bounded decoder test and one bounded inference run. CUDA UUID
and plugin GPU IDs remained pinned to GPU 0. No commit or merge is approved.

The slice now works: decoder EOS/exit 0; conventional app EOS/exit 0; 1,768
contiguous frame records; 16,848 detections, 14 empty frames; +319 (+1.93%) versus
the documented E-2 total. JSON/class/confidence/model-coordinate checks pass.
Read report.md for class counts, hashes, mount contract and full limitations.

Device exposure is the decisive observed variable: the same native probe fails
with NVDEC error 100 under `--gpus device=0` and succeeds under `--gpus all`,
with CUDA seeing one device in both cases. Broader exposure is an approved
experiment, not a permanent isolation policy. No host driver or shared service
was changed. A post-run GPU sample retained ComfyUI on GPU 0 and llama-server
on GPU 1; the short app had already exited, so this is not continuous
utilization/isolation measurement.

## Files and evidence

Changed/prepared text files are this handoff, report, README, parser source,
parser contract tests, build/run scripts, native capability probe, COCO labels
and separate app/inference configs. Originals in the develop worktree are
preserved. Footage and engine there were mounted read-only.

Exact task-owned file inventory under `benchmarks/e-3/`:

```text
HANDOFF.md
README.md
report.md
build_parser.sh
config_infer_primary_yolo26n.txt
labels.txt
nvdsinfer_custom_yolo26n.cpp
probe_nvdec.py
run_yolo26n_one_stream.sh
t1-yolo26n-one-stream.txt
test_yolo26n_parser.cpp
```

- Successful output: `/tmp/e3-t1-yolo26n.gmTW5U/detections.jsonl`.
- Successful full log: `/tmp/e3-t1-yolo26n.gmTW5U/pipeline.log`.
- Decoder success: `/tmp/e3-t1-decode-all-visible.log`.
- Controlled probes: `/tmp/e3-t1-nvdec-all-visible.log` and
  `/tmp/e3-t1-nvdec-single-visible.log`.
- Runtime parser binary: `/tmp/e3-t1-parser.FzdNLS/libnvdsinfer_custom_yolo26n.so`.
- Byte-identical tested rebuild and full build log:
  `/tmp/e3-t1-parser-tests.oQcJov/`.
- Python gate output: `/tmp/e3-t1-make-gates.log`.

The paths above are the original temporary locations; key evidence now also
exists in the ignored archive named at the top of this handoff. Prior stalled-run
logs remain under `/tmp/e3-t1-*` as listed in the report. Test containers have
completed or been stopped. Third-party FFmpeg/footage comparison was rejected
by automatic review and not executed; host syscall attach was unavailable.

## Validation and limitations

- SDK build script without GPU: exit 0; eight parser contract tests pass.
  SDK header warnings remain visible.
- Decoder command: `timeout -k 3s 20s gst-launch-1.0 -e -v filesrc
  location=/tmp/crosswalk.mp4 ! qtdemux ! h264parse ! nvv4l2decoder gpu-id=0 !
  fakesink sync=false`; exit 0, EOS, 2.666921798 s processing interval.
- App command: `timeout -k 5s 120s /tmp/run_yolo26n_one_stream.sh`;
  exit 0, EOS and non-empty detections.
- Host make used a containerized PYTHON command to run each separate gate in
  `ava/dev:local` (full exact commands in `/tmp/e3-t1-make-gates.log`).
  Unit: 259 passed; integration: 1 passed; coverage: 260 passed, 94.40%;
  lint: passed. E2e: no tests collected, pytest exit 5. Infra: seven skipped
  because Docker is unavailable inside the test container.
- The initial in-container make attempt failed because that image has no make;
  host make with containerized Python supplied the successful commands.
- Shell syntax and tracked whitespace checks passed.
- Separate probe lint, probe syntax, all new-file whitespace and synthetic
  parser JSONL checks passed. No named experiment containers remain.

The local E-2 JSONL is not the historical baseline: 16,811 detections and all
track IDs null. Do not substitute it for the documented 16,529/267 artifact.
No labelled accuracy pass, tracker/rule parity, inference timing, throughput or
capacity claim is established. Tensor-shape validation is not generic; the parser
is specific to the supplied artifact. JSONL contains model boxes and parser
sequence, not source PTS or tracker IDs. Pipeline performance reporting did not
fire before this short run finished.

## Exact next action

Review the uncommitted slice and validation. Commit requires human approval
of the exact full diff and message. Full T1 remains open for YOLOE/profile-change
behavior, fuller comparison and compatibility go/no-go. A materially different
subtask requires a new QA/plan-confirmation gate. Do not repeat stalled runs,
permanently broaden GPU access or start YOLOE under this experiment's approval.

Private progress and E-3 plan are updated to record conventional success while
keeping T1 incomplete. No repository history has been written.
