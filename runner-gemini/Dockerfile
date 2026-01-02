# LazyAF Runner - Gemini
# Build: docker build -t lazyaf-runner-gemini:latest ./runner-gemini
FROM python:3.12-slim

# Install system dependencies including sudo
RUN apt-get update && apt-get install -y \
    git \
    curl \
    gh \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Gemini CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Gemini CLI
RUN npm install -g @google/gemini-cli

# Install Python dependencies
RUN pip install --no-cache-dir requests

# Create non-root user for running Gemini CLI
RUN useradd -m -s /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Create workspace directory and give ownership to runner
RUN mkdir -p /workspace && chown -R runner:runner /workspace

# Create /app/data for tests that import backend code (git_server expects this)
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
ENV RUNNER_TYPE="gemini"
ENV RUNNER_NAME=""
ENV POLL_INTERVAL="5"
ENV GEMINI_API_KEY=""
ENV GITHUB_TOKEN=""

ENTRYPOINT ["python", "/entrypoint.py"]
