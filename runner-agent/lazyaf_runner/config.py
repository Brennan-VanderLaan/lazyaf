"""
Configuration for LazyAF Runner Agent.

Configuration can be provided via:
- Environment variables
- Constructor arguments
- Config file (future)
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunnerConfig:
    """
    Runner agent configuration.

    Attributes:
        backend_url: URL of LazyAF backend (e.g., "http://localhost:8000")
        runner_id: Unique runner ID (auto-generated if not provided)
        runner_name: Human-readable runner name
        runner_type: Runner type (claude-code, gemini, generic)
        labels: Labels for requirement matching
        heartbeat_interval: Seconds between heartbeats
        reconnect_delay: Seconds to wait before reconnecting
    """
    backend_url: str = field(
        default_factory=lambda: os.getenv("LAZYAF_BACKEND_URL", "http://localhost:8000")
    )
    runner_id: Optional[str] = field(
        default_factory=lambda: os.getenv("LAZYAF_RUNNER_ID")
    )
    runner_name: str = field(
        default_factory=lambda: os.getenv("LAZYAF_RUNNER_NAME", "LazyAF Runner")
    )
    runner_type: str = field(
        default_factory=lambda: os.getenv("LAZYAF_RUNNER_TYPE", "claude-code")
    )
    labels: dict = field(default_factory=dict)
    heartbeat_interval: int = 10
    reconnect_delay: int = 5

    def __post_init__(self):
        """Parse environment variables for labels."""
        # Parse LAZYAF_RUNNER_LABELS if set (format: key=value,key2=value2)
        labels_env = os.getenv("LAZYAF_RUNNER_LABELS", "")
        if labels_env and not self.labels:
            self.labels = {}
            for item in labels_env.split(","):
                if "=" in item:
                    key, value = item.split("=", 1)
                    # Handle list values (e.g., has=gpio:camera)
                    if ":" in value:
                        self.labels[key.strip()] = value.strip().split(":")
                    else:
                        self.labels[key.strip()] = value.strip()

    @property
    def websocket_url(self) -> str:
        """Get WebSocket URL from backend URL."""
        url = self.backend_url
        if url.startswith("https://"):
            return url.replace("https://", "wss://") + "/ws/runner"
        elif url.startswith("http://"):
            return url.replace("http://", "ws://") + "/ws/runner"
        else:
            return f"ws://{url}/ws/runner"
