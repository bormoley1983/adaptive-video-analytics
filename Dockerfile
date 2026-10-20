# AVA inference image.
#
# Base: NVIDIA CUDA runtime image so the container sees the host GPU via the
# NVIDIA Container Toolkit. The Python version is chosen for compatibility with
# current stable PyTorch/Ultralytics wheels, independently of host Python 3.14.4
# (ADR-1). Revalidate and pin the exact tag after the compatibility run.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

# System libraries needed by OpenCV/FFmpeg decode and the Python toolchain.
# Python 3.12 is selected because torch 2.14's _dynamo requires
# sys.get_int_max_str_digits (added in 3.12); 3.11 fails at import time.
# This is the newest mutually supported stable interpreter (ADR-1).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3-pip \
        git ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package and its locked dependencies. The git+CLIP dependency is
# fetched at build time so inference never triggers a first-use pip install.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3.12 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

ENV PATH="/opt/venv/bin:$PATH"

# Model and asset caches live in a dedicated dir that Compose mounts persistently.
ENV AVA_MODEL_DIR=/models
RUN mkdir -p /models /results /videos

ENTRYPOINT ["ava"]
CMD ["--help"]
