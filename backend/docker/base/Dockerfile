# LazyAF Base Image - Phase 12.3
# Foundation for all step execution containers

FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create workspace directories
RUN mkdir -p /workspace/repo /workspace/home /workspace/.control

# Create control layer directory
RUN mkdir -p /control

# Copy control layer script
COPY control/run.py /control/run.py

# Set working directory
WORKDIR /workspace/repo

# Set environment variables for HOME persistence
ENV HOME=/workspace/home
ENV XDG_CACHE_HOME=/workspace/home/.cache
ENV XDG_CONFIG_HOME=/workspace/home/.config
ENV XDG_DATA_HOME=/workspace/home/.local/share
ENV PIP_CACHE_DIR=/workspace/home/.cache/pip
ENV PIP_USER=1
ENV PYTHONUSERBASE=/workspace/home/.local
ENV PATH=/workspace/home/.local/bin:/workspace/home/.npm-global/bin:$PATH

# Default entrypoint runs control layer
ENTRYPOINT ["python", "/control/run.py"]
