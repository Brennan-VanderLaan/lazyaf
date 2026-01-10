"""
Mock executor implementation for E2E testing.

This executor simulates AI behavior with deterministic responses,
allowing full E2E testing without invoking real AI services.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Any

from .base import AgentExecutor, ExecutorConfig, ExecutorResult


@dataclass
class MockConfig:
    """Configuration for mock executor behavior."""

    response_mode: str = "batch"
    """'streaming' or 'batch' - how to emit output events."""

    delay_ms: int = 100
    """Delay between output events in milliseconds."""

    file_operations: list = field(default_factory=list)
    """List of file operations to perform."""

    output_events: list = field(default_factory=list)
    """List of output events to emit."""

    exit_code: int = 0
    """Exit code to return."""

    error_message: Optional[str] = None
    """Error message if exit_code != 0."""


class MockExecutor(AgentExecutor):
    """
    Executor that simulates AI behavior for testing.

    Instead of invoking a CLI, this executor:
    1. Loads mock configuration from step_config or workspace
    2. Applies file operations (create, modify, delete)
    3. Streams simulated output events
    4. Returns configured exit code

    Mock config format:
    {
        "response_mode": "streaming" | "batch",
        "delay_ms": 100,
        "file_operations": [
            {"action": "create", "path": "src/new.py", "content": "..."},
            {"action": "modify", "path": "src/old.py", "search": "x", "replace": "y"},
            {"action": "delete", "path": "src/obsolete.py"},
            {"action": "append", "path": "README.md", "content": "..."}
        ],
        "output_events": [
            {"type": "content", "text": "Analyzing..."},
            {"type": "tool_use", "tool": "Read", "path": "main.py"},
            {"type": "complete", "text": "Done!"}
        ],
        "exit_code": 0
    }
    """

    def __init__(self, mock_config: Optional[dict] = None):
        """
        Initialize mock executor.

        Args:
            mock_config: Optional mock configuration. If not provided,
                        will be loaded from workspace or step_config.
        """
        self._mock_config = mock_config

    @property
    def name(self) -> str:
        return "Mock"

    @property
    def runner_type(self) -> str:
        return "mock"

    def build_command(self, config: ExecutorConfig) -> list[str]:
        """Mock executor doesn't use a CLI command."""
        return ["echo", "mock-executor"]

    def execute(
        self,
        config: ExecutorConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        streaming: bool = True,
        mock_config: Optional[dict] = None,
    ) -> ExecutorResult:
        """
        Execute mock behavior.

        Args:
            config: Executor configuration (workspace, prompt).
            log_callback: Optional callback for log lines.
            streaming: If True, add delays between output events.
            mock_config: Optional mock config override.

        Returns:
            ExecutorResult with configured success/failure.
        """
        try:
            # Load mock configuration
            mc = mock_config or self._mock_config
            if mc is None:
                mc = self._load_mock_config(config.workspace, log_callback)

            mock = self._parse_mock_config(mc)

            if log_callback:
                log_callback("[mock] Starting mock execution")

            # Apply file operations
            if mock.file_operations:
                self._apply_file_operations(
                    config.workspace,
                    mock.file_operations,
                    log_callback,
                )

            # Stream output events
            if mock.output_events:
                self._stream_output_events(
                    mock.output_events,
                    mock.delay_ms if streaming else 0,
                    log_callback,
                )

            # Return result based on configured exit code
            if mock.exit_code != 0:
                error = mock.error_message or f"Mock executor failed with exit code {mock.exit_code}"
                if log_callback:
                    log_callback(f"[mock] Error: {error}")
                return ExecutorResult(
                    success=False,
                    exit_code=mock.exit_code,
                    error=error,
                )

            if log_callback:
                log_callback("[mock] Mock execution completed successfully")

            return ExecutorResult(
                success=True,
                exit_code=0,
            )

        except Exception as e:
            if log_callback:
                log_callback(f"[mock] Exception: {e}")
            return ExecutorResult(
                success=False,
                exit_code=-1,
                error=str(e),
            )

    def _load_mock_config(
        self,
        workspace: Path,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Load mock configuration from workspace."""
        # Check for mock config file in workspace
        config_path = workspace / ".control" / "mock_config.json"
        if config_path.exists():
            if log_callback:
                log_callback(f"[mock] Loading config from {config_path}")
            return json.loads(config_path.read_text())

        # Default mock config
        if log_callback:
            log_callback("[mock] Using default config")
        return {
            "response_mode": "batch",
            "delay_ms": 50,
            "file_operations": [
                {
                    "action": "create",
                    "path": ".lazyaf-mock-marker",
                    "content": f"# Mock executor ran at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                }
            ],
            "output_events": [
                {"type": "content", "text": "Mock executor starting..."},
                {"type": "content", "text": "Applying mock file operations..."},
                {"type": "complete", "text": "Mock execution complete."}
            ],
            "exit_code": 0
        }

    def _parse_mock_config(self, config: dict) -> MockConfig:
        """Parse mock config dict into MockConfig dataclass."""
        return MockConfig(
            response_mode=config.get("response_mode", "batch"),
            delay_ms=config.get("delay_ms", 100),
            file_operations=config.get("file_operations", []),
            output_events=config.get("output_events", []),
            exit_code=config.get("exit_code", 0),
            error_message=config.get("error_message"),
        )

    def _apply_file_operations(
        self,
        workspace: Path,
        operations: list[dict],
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Apply file operations as specified in mock config."""
        for op in operations:
            action = op.get("action", "")
            path = op.get("path", "")

            if not path:
                if log_callback:
                    log_callback(f"[mock] Skipping operation with no path: {op}")
                continue

            file_path = workspace / path

            if action == "create":
                if log_callback:
                    log_callback(f"[mock] Creating file: {path}")
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(op.get("content", ""))

            elif action == "modify":
                if not file_path.exists():
                    if log_callback:
                        log_callback(f"[mock] File does not exist, creating: {path}")
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(op.get("content", ""))
                else:
                    search = op.get("search", "")
                    replace = op.get("replace", "")
                    if search:
                        if log_callback:
                            log_callback(f"[mock] Modifying file: {path} (search/replace)")
                        content = file_path.read_text()
                        content = content.replace(search, replace)
                        file_path.write_text(content)
                    elif "content" in op:
                        if log_callback:
                            log_callback(f"[mock] Modifying file: {path} (overwrite)")
                        file_path.write_text(op.get("content", ""))

            elif action == "delete":
                if file_path.exists():
                    if log_callback:
                        log_callback(f"[mock] Deleting file: {path}")
                    file_path.unlink()
                else:
                    if log_callback:
                        log_callback(f"[mock] File to delete not found: {path}")

            elif action == "append":
                if log_callback:
                    log_callback(f"[mock] Appending to file: {path}")
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, "a") as f:
                    f.write(op.get("content", ""))

            else:
                if log_callback:
                    log_callback(f"[mock] Unknown action: {action}")

    def _stream_output_events(
        self,
        events: list[dict],
        delay_ms: int = 100,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Stream output events with delays to simulate real AI behavior."""
        delay_sec = delay_ms / 1000.0

        for event in events:
            event_type = event.get("type", "content")

            if event_type == "content":
                text = event.get("text", "")
                if log_callback:
                    log_callback(f"[AI] {text}")

            elif event_type == "tool_use":
                tool = event.get("tool", "Unknown")
                path = event.get("path", "")
                if log_callback:
                    log_callback(f"[AI] Using tool: {tool}" + (f" on {path}" if path else ""))

            elif event_type == "complete":
                text = event.get("text", "Complete")
                if log_callback:
                    log_callback(f"[AI] {text}")

            if delay_ms > 0:
                time.sleep(delay_sec)
