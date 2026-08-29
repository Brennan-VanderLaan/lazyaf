"""
Step Configuration Parser.

Reads and parses step configuration from /workspace/.control/step_config.json.

The file is produced VERBATIM by the backend's
``app.services.control_layer.workspace.generate_step_config`` — that producer
is the single source of truth for the shape (R3). Key names here
(``auth_token``, ``working_directory``, ``command`` as a string) must match it;
the producer<->consumer round-trip is pinned by
``tdd/unit/control_runtime/test_config_contract.py``.
"""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class StepConfig:
    """Step execution configuration."""

    # Required fields (producer contract)
    step_id: str
    backend_url: str
    auth_token: str
    command: str  # raw user command STRING; shell-wrapped by the executor

    # Producer-supplied identifiers (informational for the runtime)
    step_run_id: str = ""
    execution_key: str = ""

    # Optional fields with defaults
    working_directory: str = "/workspace/repo"
    environment: Dict[str, str] = field(default_factory=dict)
    # Producer sends an int; the consumer tolerates any positive number
    # (floats keep the timeout watchdog unit-testable in sub-second time).
    timeout_seconds: float = 3600
    shell: str = "bash"

    # NOTE: heartbeat interval and log batching knobs are intentionally NOT
    # config fields — they are module constants (heartbeat.HEARTBEAT_INTERVAL,
    # executor.LOG_BATCH_SIZE, executor.LOG_BATCH_INTERVAL). The producer never
    # transported them; phantom consumer-side fields would drift silently.


def _fail(reason: str) -> None:
    print(f"[control] ERROR: invalid step config: {reason}", file=sys.stderr)


def load_step_config(config_path: Path) -> Optional[StepConfig]:
    """
    Load step configuration from JSON file.

    Args:
        config_path: Path to step_config.json

    Returns:
        StepConfig if successful, None if file missing or invalid
        (the reason is printed to stderr — never a silent None).
    """
    try:
        if not config_path.exists():
            _fail(f"config file not found: {config_path}")
            return None

        with open(config_path, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        _fail(f"config is not valid JSON: {e}")
        return None
    except OSError as e:
        _fail(f"could not read config file: {e}")
        return None

    for key in ("step_id", "backend_url", "auth_token", "command"):
        if not data.get(key):
            _fail(f"missing required key: {key}")
            return None

    command = data["command"]
    if not isinstance(command, str):
        _fail(
            "command must be a string (the raw user script); "
            f"got {type(command).__name__}"
        )
        return None

    return StepConfig(
        step_id=data["step_id"],
        backend_url=data["backend_url"],
        auth_token=data["auth_token"],
        command=command,
        step_run_id=data.get("step_run_id", ""),
        execution_key=data.get("execution_key", ""),
        working_directory=data.get("working_directory", "/workspace/repo"),
        environment=data.get("environment", {}),
        timeout_seconds=data.get("timeout_seconds", 3600),
        shell=data.get("shell", "bash"),
    )
