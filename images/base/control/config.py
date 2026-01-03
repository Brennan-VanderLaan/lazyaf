"""
Step Configuration Parser.

Reads and parses step configuration from /workspace/.control/step_config.json.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List


@dataclass
class StepConfig:
    """Step execution configuration."""

    # Required fields
    step_id: str
    backend_url: str
    token: str
    command: List[str]

    # Optional fields with defaults
    working_dir: str = "/workspace/repo"
    environment: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 3600
    heartbeat_interval: float = 10.0
    log_batch_size: int = 100
    log_batch_interval: float = 1.0


def load_step_config(config_path: Path) -> Optional[StepConfig]:
    """
    Load step configuration from JSON file.

    Args:
        config_path: Path to step_config.json

    Returns:
        StepConfig if successful, None if file missing or invalid
    """
    try:
        if not config_path.exists():
            return None

        with open(config_path, "r") as f:
            data = json.load(f)

        # Required fields
        step_id = data.get("step_id")
        backend_url = data.get("backend_url")
        token = data.get("token")
        command = data.get("command")

        if not all([step_id, backend_url, token, command]):
            return None

        # Build config with defaults
        return StepConfig(
            step_id=step_id,
            backend_url=backend_url,
            token=token,
            command=command,
            working_dir=data.get("working_dir", "/workspace/repo"),
            environment=data.get("environment", {}),
            timeout_seconds=data.get("timeout_seconds", 3600),
            heartbeat_interval=data.get("heartbeat_interval", 10.0),
            log_batch_size=data.get("log_batch_size", 100),
            log_batch_interval=data.get("log_batch_interval", 1.0),
        )

    except (json.JSONDecodeError, IOError, KeyError):
        return None
