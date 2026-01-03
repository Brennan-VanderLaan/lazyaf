# LazyAF Base Image
# Build: docker build -t lazyaf-base:latest ./images/base
#
# This image provides:
# - Python 3.12 runtime
# - Git and common tools
# - Control layer for backend communication
# - HOME persistence at /workspace/home

FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for running commands
# Many tools refuse to run as root for safety
RUN useradd -m -s /bin/bash lazyaf \
    && echo "lazyaf ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Create directory structure
# /workspace - mounted volume with repo and home
# /workspace/repo - git repository for step execution
# /workspace/home - persistent HOME directory
# /workspace/.control - step config and communication
# /control - control layer scripts (built into image)
RUN mkdir -p /workspace/repo /workspace/home /workspace/.control /control \
    && chown -R lazyaf:lazyaf /workspace

# Install control layer Python dependencies
COPY requirements.txt /control/
RUN pip install --no-cache-dir -r /control/requirements.txt

# Copy control layer scripts
COPY control/ /control/
RUN chmod +x /control/run.py

# Set HOME to workspace for cache persistence across steps
# This allows pip, npm, uv, etc. to cache in the mounted volume
ENV HOME=/workspace/home
ENV PATH="/workspace/home/.local/bin:$PATH"

# Working directory defaults to repo location
WORKDIR /workspace/repo

# Switch to non-root user
USER lazyaf

# Control layer is the entrypoint
# It reads /workspace/.control/step_config.json and executes the command
ENTRYPOINT ["python", "/control/run.py"]
