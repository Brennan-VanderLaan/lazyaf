"""
Control Layer Protocol - Phase 12.3

Defines the communication protocol between step containers and the backend:
- StepConfig: Configuration read from /workspace/.control/step_config.json
- ControlLayerClient: HTTP client for reporting to backend
- StepExecutor: Runs commands and captures output
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StepTimeoutError(Exception):
    """Raised when step execution times out."""
    pass


@dataclass
class StepConfig:
    """Configuration for a step execution."""
    step_id: str
    step_run_id: str
    execution_key: str
    command: str
    backend_url: str
    auth_token: str
    environment: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 3600
    working_directory: str = "/workspace/repo"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StepConfig":
        """Create StepConfig from dictionary."""
        return cls(
            step_id=data["step_id"],
            step_run_id=data["step_run_id"],
            execution_key=data["execution_key"],
            command=data["command"],
            backend_url=data["backend_url"],
            auth_token=data["auth_token"],
            environment=data.get("environment", {}),
            timeout_seconds=data.get("timeout_seconds", 3600),
            working_directory=data.get("working_directory", "/workspace/repo"),
        )

    @classmethod
    def from_file(cls, path: Path) -> "StepConfig":
        """Load StepConfig from JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls.from_dict(data)


@dataclass
class StepResult:
    """Result of step execution."""
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class ControlLayerClient:
    """
    HTTP client for container-to-backend communication.

    Handles:
    - Status reporting
    - Log streaming
    - Heartbeats
    - Retry logic for transient failures
    """

    def __init__(
        self,
        backend_url: str,
        auth_token: str,
        step_id: str,
        http_client: Any = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        heartbeat_interval: float = 30.0,
        log_batch_size: int = 10,
    ):
        self.backend_url = backend_url.rstrip("/")
        self.auth_token = auth_token
        self.step_id = step_id
        self._http_client = http_client
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.heartbeat_interval = heartbeat_interval
        self.log_batch_size = log_batch_size

        # Log buffering
        self._log_buffer: List[Dict[str, Any]] = []
        self._pending_logs: List[str] = []

    @property
    def pending_log_count(self) -> int:
        """Number of pending log lines."""
        return len(self._pending_logs)

    def _get_headers(self) -> Dict[str, str]:
        """Get headers with auth token."""
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> Any:
        """Make HTTP request with retry logic."""
        last_error = None

        for attempt in range(self.max_retries):
            try:
                if method == "POST":
                    response = await self._http_client.post(url, **kwargs)
                else:
                    response = await self._http_client.get(url, **kwargs)
                return response
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Request failed (attempt {attempt + 1}/{self.max_retries}): {e}"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)

        # All retries failed - log but don't raise
        logger.error(f"All retries failed for {url}: {last_error}")
        return None

    async def report_status(
        self,
        status: str,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Report step status to backend.

        Args:
            status: Step status (running, completed, failed)
            exit_code: Exit code if completed/failed
            error: Error message if failed
        """
        url = f"{self.backend_url}/api/steps/{self.step_id}/status"
        payload = {
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if exit_code is not None:
            payload["exit_code"] = exit_code
        if error is not None:
            payload["error"] = error

        await self._request_with_retry(
            "POST",
            url,
            json=payload,
            headers=self._get_headers(),
        )

    async def send_logs(
        self,
        content: str,
        stream: str = "stdout",
    ) -> None:
        """
        Send log content to backend.

        Args:
            content: Log content
            stream: Stream name (stdout or stderr)
        """
        url = f"{self.backend_url}/api/steps/{self.step_id}/logs"
        payload = {
            "content": content,
            "stream": stream,
            "timestamp": datetime.utcnow().isoformat(),
        }

        await self._request_with_retry(
            "POST",
            url,
            json=payload,
            headers=self._get_headers(),
        )

    async def queue_log_line(self, line: str, stream: str = "stdout") -> None:
        """
        Queue a log line for batched sending.

        Args:
            line: Log line content
            stream: Stream name
        """
        self._pending_logs.append(line)
        self._log_buffer.append({
            "content": line,
            "stream": stream,
            "timestamp": datetime.utcnow().isoformat(),
        })

        # Flush if buffer is full
        if len(self._log_buffer) >= self.log_batch_size:
            await self.flush_logs()

    async def flush_logs(self) -> None:
        """Flush buffered logs to backend."""
        if not self._log_buffer:
            return

        url = f"{self.backend_url}/api/steps/{self.step_id}/logs"
        payload = {"lines": self._log_buffer.copy()}

        result = await self._request_with_retry(
            "POST",
            url,
            json=payload,
            headers=self._get_headers(),
        )

        if result is not None:
            self._log_buffer.clear()
            self._pending_logs.clear()

    async def send_heartbeat(
        self,
        extend_seconds: int = 300,
        progress: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send heartbeat to extend step timeout.

        Args:
            extend_seconds: Seconds to extend timeout
            progress: Optional progress information
        """
        url = f"{self.backend_url}/api/steps/{self.step_id}/heartbeat"
        payload = {
            "extend_seconds": extend_seconds,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if progress is not None:
            payload["progress"] = progress

        await self._request_with_retry(
            "POST",
            url,
            json=payload,
            headers=self._get_headers(),
        )


class StepExecutor:
    """
    Executes step commands and captures output.

    Handles:
    - Command execution in working directory
    - stdout/stderr capture
    - Timeout enforcement
    - Environment variable setup
    """

    def __init__(
        self,
        command: str,
        working_directory: str = "/tmp",
        environment: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 3600,
        shell: bool = False,
    ):
        self.command = command
        self.working_directory = working_directory
        self.environment = environment or {}
        self.timeout_seconds = timeout_seconds
        self.shell = shell

    async def run(self) -> StepResult:
        """
        Execute the command and return result.

        Returns:
            StepResult with exit code, stdout, stderr

        Raises:
            StepTimeoutError: If execution exceeds timeout
        """
        import os
        import time

        start_time = time.time()

        # Prepare environment
        env = os.environ.copy()
        env.update(self.environment)

        process = None
        try:
            if self.shell:
                process = await asyncio.create_subprocess_shell(
                    self.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                    env=env,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *self.command.split(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                    env=env,
                )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise StepTimeoutError(
                    f"Command timed out after {self.timeout_seconds}s"
                )

            duration = time.time() - start_time

            return StepResult(
                exit_code=process.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                duration_seconds=duration,
            )

        except StepTimeoutError:
            raise
        except Exception as e:
            duration = time.time() - start_time
            return StepResult(
                exit_code=1,
                stdout="",
                stderr=str(e),
                duration_seconds=duration,
            )
        finally:
            # Give asyncio a chance to clean up transport on Windows
            if process is not None:
                try:
                    # Close transport handles explicitly
                    if process.stdout:
                        process.stdout.feed_eof()
                    if process.stderr:
                        process.stderr.feed_eof()
                except Exception:
                    pass
                # Allow event loop to process cleanup
                await asyncio.sleep(0)
