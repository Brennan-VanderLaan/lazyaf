# LazyAF Base Image (Phase 12.3)
# Build: python scripts/build_images.py   (never docker build with :latest — the
# moving local tag is lazyaf-base:dev, stamped with a content hash label)
#
# This image provides:
# - Python 3.12 runtime, git, curl
# - In-container control layer at /control (entrypoint + runtime)
# - HOME persistence at /workspace/home (baked env, works on named volumes)
#
# Contract (pinned by tdd/unit/control_runtime/test_image_contract.py):
# - LABEL lazyaf.control-layer=1 declares the control capability; the executor
#   selects control mode from this label, never from path shapes.
# - Root entrypoint /control/entrypoint.sh chowns the workspace top-level, then
#   drops to the fixed-uid `lazyaf` user (uid/gid 1000) via gosu.
# - LAZYAF_CONTROL=1 runs /control/run.py; otherwise CMD passes through, so the
#   image degrades to a stock image (the `control: false` escape hatch).

FROM python:3.12-slim

# System dependencies. gosu: drop root -> lazyaf after the entrypoint chown.
RUN apt-get update && apt-get install -y \
    git \
    curl \
    sudo \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with PINNED uid/gid 1000 — deterministic chown and tar
# ownership (put_archive writes step_config.json as 1000:1000).
# Many tools refuse to run as root for safety.
RUN groupadd -g 1000 lazyaf \
    && useradd -m -s /bin/bash -u 1000 -g 1000 lazyaf \
    && echo "lazyaf ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Directory structure
# /workspace           - mounted volume with repo and home
# /workspace/repo      - git repository for step execution
# /workspace/home      - persistent HOME directory
# /workspace/.control  - step config and communication
# /control             - control layer scripts (built into image)
RUN mkdir -p /workspace/repo /workspace/home /workspace/.control /control \
    && chown -R lazyaf:lazyaf /workspace

# Control layer Python dependencies (installed system-wide, BEFORE the
# PIP_USER env block below so they land in the image, not /workspace).
COPY requirements.txt /control/
RUN pip install --no-cache-dir -r /control/requirements.txt

# Control layer scripts
COPY control/ /control/
RUN chmod +x /control/run.py /control/entrypoint.sh

# HOME persistence env block (from the retired control_layer/environment.py —
# baked here so every child image and every mode gets it for free).
# pip, npm, uv etc. cache and install under the mounted volume and therefore
# persist across steps of a run.
ENV HOME=/workspace/home \
    XDG_CACHE_HOME=/workspace/home/.cache \
    XDG_CONFIG_HOME=/workspace/home/.config \
    XDG_DATA_HOME=/workspace/home/.local/share \
    PIP_CACHE_DIR=/workspace/home/.cache/pip \
    PIP_USER=1 \
    PYTHONUSERBASE=/workspace/home/.local \
    NPM_CONFIG_PREFIX=/workspace/home/.npm-global \
    PATH="/workspace/home/.local/bin:/workspace/home/.npm-global/bin:$PATH"

# Capability + staleness labels. content-hash is stamped by scripts/build_images.py.
ARG CONTENT_HASH=dev
LABEL lazyaf.control-layer=1
LABEL lazyaf.content-hash=$CONTENT_HASH

# Working directory defaults to repo location
WORKDIR /workspace/repo

# Root entrypoint: chown workspace top-level (fresh named volumes are
# root-owned), then gosu down to lazyaf. NO USER directive — the drop happens
# at runtime, after the chown.
ENTRYPOINT ["/control/entrypoint.sh"]
