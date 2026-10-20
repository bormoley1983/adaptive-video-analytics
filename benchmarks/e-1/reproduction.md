# E-1 manual reproduction guide

Run this on `ai-host1` from the workspace root:

```bash
cd /home/admaccess/adaptive-video-analytics
```

Everything below is copy-paste. Each section is independent — you can run just the
parts you want. Expected outputs are given so you can confirm a clean reproduction.

> **Scope note:** this reproduces the functional PoC baseline (one clip, two
> detector paths, offline batch). It is not a live-latency or detection-quality
> test. See `report.md` for the measured numbers and interpretation.

---

## 0. Prerequisites (verify once)

Confirm the host, GPU, and pre-placed assets are in place before running anything.

```bash
# Host + kernel
uname -a

# GPUs — expect TWO RTX 3090s. GPU 1 is busy with llama-server; DO NOT TOUCH it.
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total --format=csv

# Docker + Compose + NVIDIA runtime
docker --version
docker compose version
nvidia-smi -L

# Pre-placed model assets (SHA-256 must match report.md)
sha256sum models/yolo26n.pt models/yoloe-26s-seg.pt models/mobileclip2_b.ts

# Input clip (SHA-256 must match report.md)
sha256sum videos/input.mp4
```

Expected SHA-256 values:

| Asset | SHA-256 |
|---|---|
| `models/yolo26n.pt` | `9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef` |
| `models/yoloe-26s-seg.pt` | `48f24206bc8680d60cbbfa296b0140da849669b9515058b72f5a945142df0654` |
| `models/mobileclip2_b.ts` | `35d7f213e4d75f38514e4656ad3cb91158bd33e3805d8ac349f23b186f66982f` |
| `videos/input.mp4` | `110501b29df6f348f8942987a1d70e3d9f412a9b55f7b9e6c8917bcdab49f239` |

If any hash differs, stop and re-stage the asset before continuing.

---

## 1. Build the image (once)

```bash
docker compose build
```

Expected: builds `ava/e1:local` from `nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04`.
If you already built it and haven't changed `src/`, `Dockerfile`, or `pyproject.toml`,
you can skip this — but rebuild after any code change so the timing instrumentation
is present.

Confirm the image exists:

```bash
docker images ava/e1:local --digest
```

---

## 2. Run the conventional path (YOLO26n + ByteTrack)

```bash
docker compose run --rm ava run --config /configs/run-conventional.yaml \
  > /tmp/repro-conventional.log 2>&1
echo "EXIT=$?"
```

Expected: `EXIT=0`. The command prints a JSON summary to stdout (after a short
Ultralytics settings banner). Key fields to check in the JSON:

- `"frames": 1768`
- `"detections": 16811`
- phase split present: `decode_seconds`, `inference_seconds`, `output_seconds`
- steady-state latency: `infer_p50_s`, `infer_p95_s`, `infer_mean_s`
- `peak_gpu_mem_bytes` (non-zero, ~7.7e7)

View the summary:

```bash
tail -40 /tmp/repro-conventional.log
```

Confirm **no first-use downloads** happened (should print `0`):

```bash
grep -icE 'downloading' /tmp/repro-conventional.log
```

Artifacts written:

```bash
ls -la results/conventional/
# expect: detections.jsonl  annotated.mp4
wc -l results/conventional/detections.jsonl   # expect 1768 lines
```

---

## 3. Run the prompted path (YOLOE-26 small + MobileCLIP2)

```bash
docker compose run --rm ava run --config /configs/run-prompted.yaml \
  > /tmp/repro-prompted.log 2>&1
echo "EXIT=$?"
```

Expected: `EXIT=0`. JSON summary key fields:

- `"frames": 1768`
- `"detections": 16192`
- phase split + steady-state latency present
- `peak_gpu_mem_bytes` (non-zero, ~2.0e8)

View the summary and confirm no downloads:

```bash
tail -40 /tmp/repro-prompted.log
grep -icE 'downloading' /tmp/repro-prompted.log   # expect 0
```

Artifacts written:

```bash
ls -la results/prompted/
wc -l results/prompted/detections.jsonl   # expect 1768 lines
```

> **Note on the "bag" class:** the prompted run may report a `bag` class (ID 5)
> beyond the 5 configured text prompts. This is YOLOE-26s-seg's segmentation head
> emitting an extra class — expected observed behavior, not an error. See
> `report.md` → "Decisions and findings".

---

## 4. Reproducibility check (run each path a second time)

A clean reproduction means a second run of each path succeeds with **0 downloads**
and the **same detection counts**. Run both again:

```bash
docker compose run --rm ava run --config /configs/run-conventional.yaml \
  > /tmp/repro-conventional-2.log 2>&1; echo "CONV EXIT=$?"

docker compose run --rm ava run --config /configs/run-prompted.yaml \
  > /tmp/repro-prompted-2.log 2>&1; echo "PROMPTED EXIT=$?"
```

Compare detection counts between run 1 and run 2 (both should match):

```bash
echo "--- conventional ---"
grep -o '"detections": [0-9]*' /tmp/repro-conventional.log
grep -o '"detections": [0-9]*' /tmp/repro-conventional-2.log
echo "--- prompted ---"
grep -o '"detections": [0-9]*' /tmp/repro-prompted.log
grep -o '"detections": [0-9]*' /tmp/repro-prompted-2.log
```

Expected: conventional `16811` both times, prompted `16192` both times. Wall-clock
may vary by <1 s between runs (GPU scheduling/thermal) — that is normal.

---

## 5. Run the test suite

The container image does **not** include pytest, so run the tests on the host with a
throwaway venv. `ava.detector` defers its heavy imports, so only pydantic + PyYAML
are needed.

```bash
python3 -m venv /tmp/ava-test-venv
/tmp/ava-test-venv/bin/pip install -q pydantic PyYAML pytest
PYTHONPATH=src /tmp/ava-test-venv/bin/python -m pytest tests/ -q
rm -rf /tmp/ava-test-venv
```

Expected: `5 passed`. The two test modules are:

- `tests/test_config.py` — config validation (conventional/prompted round-trip, YAML load)
- `tests/test_output.py` — JSONL writer contract (one line per frame, empty frames included)

---

## 6. Quick reference (single block)

```bash
cd /home/admaccess/adaptive-video-analytics

# Build once
docker compose build

# Conventional path
docker compose run --rm ava run --config /configs/run-conventional.yaml > /tmp/repro-conventional.log 2>&1; echo "EXIT=$?"
grep -icE 'downloading' /tmp/repro-conventional.log   # expect 0
tail -40 /tmp/repro-conventional.log

# Prompted path
docker compose run --rm ava run --config /configs/run-prompted.yaml > /tmp/repro-prompted.log 2>&1; echo "EXIT=$?"
grep -icE 'downloading' /tmp/repro-prompted.log       # expect 0
tail -40 /tmp/repro-prompted.log

# Tests (host-side, throwaway venv)
python3 -m venv /tmp/ava-test-venv
/tmp/ava-test-venv/bin/pip install -q pydantic PyYAML pytest
PYTHONPATH=src /tmp/ava-test-venv/bin/python -m pytest tests/ -q   # expect 5 passed
rm -rf /tmp/ava-test-venv
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `grep downloading` prints > 0 on the **prompted** path | The pre-placed `models/mobileclip2_b.ts` is missing or its SHA differs. Re-stage it (see §0) and rerun. The Compose service resolves it via `SETTINGS["weights_dir"]`; a bare `docker run` outside Compose does not and will re-download into CWD. |
| JSON parse fails when piping to `json.load` | The Ultralytics settings banner (3 lines) is printed before the JSON. Read the file, find the first `{`, parse from there. |
| `No module named pytest` inside the container | Expected — pytest is not in the image. Run tests host-side (§5). |
| Detection counts differ from report.md | You rebuilt after a code change, or the model/config SHA differs. Verify §0 hashes and that you're on the same build as `report.md`. |
| GPU 1 memory spikes / llama-server affected | You selected the wrong GPU. Compose pins `CUDA_VISIBLE_DEVICES=0`; do not override to GPU 1. |
