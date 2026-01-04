# LazyAF Runner Agent
#
# Connects to LazyAF backend and executes steps in Docker containers.
#
# Usage:
#   docker build -t lazyaf-runner .
#   docker run -v /var/run/docker.sock:/var/run/docker.sock \
#     -e LAZYAF_BACKEND_URL=http://host.docker.internal:8000 \
#     -e LAZYAF_RUNNER_NAME="Docker Runner" \
#     lazyaf-runner

FROM python:3.12-slim

# Install Docker CLI for container management
RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source
COPY lazyaf_runner/ lazyaf_runner/

# Install the package
RUN pip install --no-cache-dir -e .

# Default command
CMD ["lazyaf-runner"]
