# Debug Sidecar Container for LazyAF Debug Re-Run Mode
#
# This container is used for interactive debugging during debug re-runs.
# It mounts the workspace volume and provides common debugging tools.
#
# Usage:
#   docker build -t lazyaf-debug-sidecar:latest .
#   docker run -it -v workspace:/workspace lazyaf-debug-sidecar:latest

FROM ubuntu:22.04

# Install debugging tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Editors
    vim \
    nano \
    less \
    # Version control
    git \
    # Network tools
    curl \
    wget \
    # System utilities
    htop \
    tree \
    jq \
    file \
    # Python (for scripts)
    python3 \
    python3-pip \
    # Shell utilities
    bash-completion \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# Set up workspace directory
WORKDIR /workspace

# Add helpful aliases
RUN echo 'alias ll="ls -la"' >> /root/.bashrc && \
    echo 'alias la="ls -A"' >> /root/.bashrc && \
    echo 'alias l="ls -CF"' >> /root/.bashrc && \
    echo 'export PS1="\[\033[01;32m\][debug]\[\033[00m\] \w\$ "' >> /root/.bashrc

# Default command - keep container running
CMD ["tail", "-f", "/dev/null"]
