#!/usr/bin/env bash
set -euo pipefail

readonly ds_dir=/opt/nvidia/deepstream/deepstream-9.2
readonly source_path=${1:-/src/nvdsinfer_custom_yolo26n.cpp}
readonly output_path=${2:-/out/libnvdsinfer_custom_yolo26n.so}
stub_dir=$(mktemp -d /tmp/ava-cuda-stub.XXXXXX)
readonly stub_dir
trap 'rm -rf -- "$stub_dir"' EXIT

printf '%s\n' \
    '#ifndef CUDA_RUNTIME_API_H' \
    '#define CUDA_RUNTIME_API_H' \
    'typedef int cudaError_t;' \
    'typedef void* cudaStream_t;' \
    'typedef void* cudaEvent_t;' \
    'typedef void* cudaGraph_t;' \
    'typedef void* cudaGraphNode_t;' \
    'struct dim3 { unsigned x, y, z; };' \
    '#endif' > "$stub_dir/cuda_runtime_api.h"

g++ -shared -std=c++17 -fPIC -Wall -Wextra \
    -I"$stub_dir" \
    -I"$ds_dir/includes" \
    "$source_path" \
    -o "$output_path"

readonly source_dir=$(dirname -- "$source_path")
readonly output_dir=$(dirname -- "$output_path")
g++ -std=c++17 -Wall -Wextra \
    -I"$stub_dir" \
    -I"$ds_dir/includes" \
    "$source_dir/test_yolo26n_parser.cpp" \
    -o "$output_dir/test_yolo26n_parser"
"$output_dir/test_yolo26n_parser" "$output_dir/parser-test-output.jsonl"
