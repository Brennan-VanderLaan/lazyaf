# LazyAF Runner - Mock Executor
# Used for E2E testing - simulates AI behavior with deterministic responses
# Build: docker build -t lazyaf-runner-mock:latest ./runner-mock
FROM python:3.12-slim

# Install system dependencies including sudo
RUN apt-get update && apt-get install -y \
    git \
    curl \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir requests

# Create non-root user (matching other runners)
RUN useradd -m -s /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Create workspace directory and give ownership to runner
RUN mkdir -p /workspace && chown -R runner:runner /workspace

# Create /app/data for tests that import backend code
RUN mkdir -p /app/data && chown -R runner:runner /app/data

# Copy entrypoint script
COPY entrypoint.py /entrypoint.py
RUN chown runner:runner /entrypoint.py

# Switch to non-root user
USER runner

# Set working directory
WORKDIR /workspace

# Environment variables (set at runtime)
ENV BACKEND_URL="http://host.docker.internal:8000"
ENV RUNNER_TYPE="mock"
ENV RUNNER_NAME=""
ENV POLL_INTERVAL="5"

ENTRYPOINT ["python", "/entrypoint.py"]
