"""
Base class and protocol for agent executors.

Each agent type (Claude, Gemini, Mock) implements this interface
to provide its specific CLI invocation logic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable


@dataclass
class ExecutorResult:
    """Result of an agent execution."""

    success: bool
    """Whether the execution succeeded (exit code 0)."""

    exit_code: int
    """The exit code from the agent CLI."""

    stdout: str = ""
    """Captured stdout from the agent."""

    stderr: str = ""
    """Captured stderr from the agent."""

    error: Optional[str] = None
    """Error message if execution failed."""


@dataclass
class ExecutorConfig:
    """Configuration for agent execution."""

    workspace: Path
    """Path to the workspace directory containing the repo."""

    prompt: str
    """The prompt to send to the agent."""

    model: Optional[str] = None
    """Optional model override."""

    agents_json: Optional[str] = None
    """Optional JSON string of agent configurations (for Claude --agents flag)."""

    timeout: Optional[int] = None
    """Optional timeout in seconds."""

    env: dict = field(default_factory=dict)
    """Additional environment variables."""


class AgentExecutor(ABC):
    """
    Abstract base class for agent executors.

    Each agent type (Claude, Gemini, Mock) provides its own implementation
    that knows how to invoke its specific CLI.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this executor."""
        pass

    @property
    @abstractmethod
    def runner_type(self) -> str:
        """Runner type identifier (e.g., 'claude-code', 'gemini', 'mock')."""
        pass

    @abstractmethod
    def build_command(self, config: ExecutorConfig) -> list[str]:
        """
        Build the CLI command to invoke the agent.

        Args:
            config: Executor configuration with prompt, model, etc.

        Returns:
            List of command arguments to execute.
        """
        pass

    def execute(
        self,
        config: ExecutorConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        streaming: bool = True,
    ) -> ExecutorResult:
        """
        Execute the agent with the given configuration.

        Args:
            config: Executor configuration.
            log_callback: Optional callback for log lines.
            streaming: If True, stream output in real-time.

        Returns:
            ExecutorResult with success status, output, and any errors.
        """
        import subprocess

        cmd = self.build_command(config)

        if log_callback:
            log_callback(f"$ {' '.join(cmd)}")

        try:
            if streaming:
                return self._execute_streaming(cmd, config, log_callback)
            else:
                return self._execute_blocking(cmd, config, log_callback)
        except Exception as e:
            return ExecutorResult(
                success=False,
                exit_code=-1,
                error=str(e),
            )

    def _execute_blocking(
        self,
        cmd: list[str],
        config: ExecutorConfig,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> ExecutorResult:
        """Execute command and wait for completion."""
        import subprocess

        result = subprocess.run(
            cmd,
            cwd=str(config.workspace),
            capture_output=True,
            text=True,
            timeout=config.timeout,
            env={**dict(__import__('os').environ), **config.env} if config.env else None,
        )

        if log_callback:
            for line in result.stdout.strip().split('\n'):
                if line:
                    log_callback(f"  {line}")
            for line in result.stderr.strip().split('\n'):
                if line:
                    log_callback(f"  [stderr] {line}")

        return ExecutorResult(
            success=result.returncode == 0,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            error=f"Exit code {result.returncode}" if result.returncode != 0 else None,
        )

    def _execute_streaming(
        self,
        cmd: list[str],
        config: ExecutorConfig,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> ExecutorResult:
        """Execute command with real-time output streaming."""
        import subprocess
        import os
        import select
        import sys

        stdout_lines = []
        stderr_lines = []

        env = {**os.environ, **config.env} if config.env else None

        process = subprocess.Popen(
            cmd,
            cwd=str(config.workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

        try:
            # On Windows, we can't use select, so fall back to blocking reads
            if sys.platform == 'win32':
                stdout, stderr = process.communicate(timeout=config.timeout)
                stdout_lines = stdout.split('\n') if stdout else []
                stderr_lines = stderr.split('\n') if stderr else []
                if log_callback:
                    for line in stdout_lines:
                        if line:
                            log_callback(f"  {line}")
                    for line in stderr_lines:
                        if line:
                            log_callback(f"  [stderr] {line}")
            else:
                # Unix: use select for real-time streaming
                while True:
                    reads = [process.stdout, process.stderr]
                    readable, _, _ = select.select(reads, [], [], 0.1)

                    for stream in readable:
                        line = stream.readline()
                        if line:
                            line = line.rstrip('\n')
                            if stream == process.stdout:
                                stdout_lines.append(line)
                                if log_callback:
                                    log_callback(f"  {line}")
                            else:
                                stderr_lines.append(line)
                                if log_callback:
                                    log_callback(f"  [stderr] {line}")

                    if process.poll() is not None:
                        # Process finished, read remaining output
                        for line in process.stdout:
                            line = line.rstrip('\n')
                            stdout_lines.append(line)
                            if log_callback and line:
                                log_callback(f"  {line}")
                        for line in process.stderr:
                            line = line.rstrip('\n')
                            stderr_lines.append(line)
                            if log_callback and line:
                                log_callback(f"  [stderr] {line}")
                        break
        except subprocess.TimeoutExpired:
            process.kill()
            return ExecutorResult(
                success=False,
                exit_code=-1,
                stdout='\n'.join(stdout_lines),
                stderr='\n'.join(stderr_lines),
                error=f"Timeout after {config.timeout} seconds",
            )

        return ExecutorResult(
            success=process.returncode == 0,
            exit_code=process.returncode,
            stdout='\n'.join(stdout_lines),
            stderr='\n'.join(stderr_lines),
            error=f"Exit code {process.returncode}" if process.returncode != 0 else None,
        )
