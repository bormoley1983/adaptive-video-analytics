#!/usr/bin/env bash
# Download and verify the E-1 model assets (data only; no code changes).
# Links and SHA-256 values from docs/dev-plan/E-1-prerequisites.md.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p models

declare -A ASSETS=(
  [yolo26n.pt]="9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"
  [yoloe-26s-seg.pt]="48f24206bc8680d60cbbfa296b0140da849669b9515058b72f5a945142df0654"
  [mobileclip2_b.ts]="35d7f213e4d75f38514e4656ad3cb91158bd33e3805d8ac349f23b186f66982f"
)

for name in "${!ASSETS[@]}"; do
  url="https://github.com/ultralytics/assets/releases/download/v8.4.0/${name}"
  if [[ -f "models/${name}" ]]; then
    echo "exists: models/${name}"
  else
    curl --fail --location --continue-at - --retry 3 \
      --output "models/${name}" "${url}"
  fi
done

echo "--- verifying SHA-256 ---"
sha256sum models/yolo26n.pt models/yoloe-26s-seg.pt models/mobileclip2_b.ts
