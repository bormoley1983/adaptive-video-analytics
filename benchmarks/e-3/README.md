# DeepStream compatibility experiment

The [report](report.md) records a successful conventional YOLO26n one-stream
run: 1,768 frame records, 16,848 detections and EOS. T1 remains open for the
other model and acceptance gates.

## Device visibility

On this host/image, `--gpus device=0` makes native NVDEC return error 100 even
though CUDA initializes. The owner-approved experiment succeeded using
`--gpus all` together with
`CUDA_VISIBLE_DEVICES=GPU-facd1ddc-c4ab-7ac9-0000-ead909e0ec42` and all SDK
plugin GPU IDs set to 0. This broader device exposure requires owner approval;
it is not a default deployment setting or a guarantee of GPU isolation.

`probe_nvdec.py` queries H.264 NVDEC initialization without footage. Mount it
read-only into the pinned image and run it with `python3`. Restrict CUDA to the
selected UUID: the probe refuses more than one CUDA-visible device and exits
nonzero on API failure. A successful query does not certify decoding.

## Build and validate

Build inside the pinned DeepStream 9.2 image from the report, without exposing
a GPU. Mount this directory read-only at `/src` and a fresh local output
directory at `/out`, then execute `/src/build_parser.sh`.

The script builds `libnvdsinfer_custom_yolo26n.so`, compiles and runs eight CPU
contract tests, and writes the generated library, test executable and synthetic
test JSONL under `/out`. NVIDIA SDK headers emit warnings. The CUDA declaration
stub supplies compile-time declarations only; it is not a CUDA runtime.

## Run the conventional profile

With approved device visibility, CUDA pinned to GPU 0 and a current allocation
check, mount these inputs read-only:

- crosswalk media at `/tmp/crosswalk.mp4`;
- the verified 640-input engine at `/tmp/yolo26n.engine`;
- the built library at `/tmp/libnvdsinfer_custom_yolo26n.so`;
- `labels.txt`, `config_infer_primary_yolo26n.txt` and
  `t1-yolo26n-one-stream.txt` under `/tmp/` with their original filenames;
- `run_yolo26n_one_stream.sh` at `/tmp/run_yolo26n_one_stream.sh`.

Mount a fresh writable results directory at `/tmp/results`, then execute
`timeout -k 5s 120s /tmp/run_yolo26n_one_stream.sh` inside the pinned container.
Capture the complete log. The timeout belongs inside the container; terminating
only the Docker client can leave its workload running.

The script defaults `AVA_DETECTION_JSONL` to `/tmp/results/detections.jsonl`.
Parser output appends and includes empty frames. Sequence numbers are parser
invocation order, not source timestamps; boxes are in model coordinates.
The profile disables tracking, display and file looping. A successful run needs
EOS and inspectable detections. Validate the output tensor before using another
export: this parser assumes float `(1,84,8400)`, applies per-class thresholds,
class-aware NMS at IoU 0.5 and a global 200-object cap.

Generated libraries, models, engines, media and results stay outside Git.
