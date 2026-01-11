#!/usr/bin/env python3
"""
LazyAF Control Layer - Phase 12.3

Container entrypoint that manages step execution lifecycle.
"""
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import httpx
except ImportError:
    print("[control] httpx not available, installing...", file=sys.stderr)
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx", "-q"])
    import httpx


# Configuration
CONFIG_PATH = Path("/workspace/.control/step_config.json")
HEARTBEAT_INTERVAL = 30  # seconds
LOG_BATCH_SIZE = 10
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class ControlLayer:
    """Main control layer implementation."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.step_id = config["step_id"]
        self.backend_url = config["backend_url"].rstrip("/")
        self.auth_token = config["auth_token"]
        self.command = config["command"]
        self.timeout_seconds = config.get("timeout_seconds", 3600)
        self.working_directory = config.get("working_directory", "/workspace/repo")
        self.environment = config.get("environment", {})

        self._process: Optional[asyncio.subprocess.Process] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._shutdown = False

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> Optional[httpx.Response]:
        """Make HTTP request with retry logic."""
        url = f"{self.backend_url}{endpoint}"

        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    if method == "POST":
                        response = await client.post(url, **kwargs)
                    else:
                        response = await client.get(url, **kwargs)
                    return response
            except Exception as e:
                print(f"[control] Request failed (attempt {attempt + 1}): {e}", file=sys.stderr)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)

        return None

    async def report_status(self, status: str, exit_code: int = None, error: str = None):
        """Report step status to backend."""
        payload = {
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if error:
            payload["error"] = error

        await self._request(
            "POST",
            f"/api/steps/{self.step_id}/status",
            json=payload,
            headers=self._get_headers(),
        )

    async def send_logs(self, content: str, stream: str = "stdout"):
        """Send log content to backend."""
        payload = {
            "content": content,
            "stream": stream,
            "timestamp": datetime.utcnow().isoformat(),
        }

        await self._request(
            "POST",
            f"/api/steps/{self.step_id}/logs",
            json=payload,
            headers=self._get_headers(),
        )

    async def send_heartbeat(self):
        """Send heartbeat to extend timeout."""
        payload = {
            "extend_seconds": HEARTBEAT_INTERVAL * 2,
            "timestamp": datetime.utcnow().isoformat(),
        }

        await self._request(
            "POST",
            f"/api/steps/{self.step_id}/heartbeat",
            json=payload,
            headers=self._get_headers(),
        )

    async def _heartbeat_loop(self):
        """Background task to send periodic heartbeats."""
        while not self._shutdown:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self._shutdown:
                    await self.send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[control] Heartbeat error: {e}", file=sys.stderr)

    async def _stream_output(self, stream: asyncio.StreamReader, name: str):
        """Stream output to backend."""
        buffer = []

        while True:
            try:
                line = await stream.readline()
                if not line:
                    break

                decoded = line.decode("utf-8", errors="replace")
                print(decoded, end="", file=sys.stdout if name == "stdout" else sys.stderr)
                buffer.append(decoded)

                if len(buffer) >= LOG_BATCH_SIZE:
                    await self.send_logs("".join(buffer), name)
                    buffer.clear()

            except Exception as e:
                print(f"[control] Stream error: {e}", file=sys.stderr)
                break

        # Flush remaining
        if buffer:
            await self.send_logs("".join(buffer), name)

    async def run(self) -> int:
        """Execute the step and return exit code."""
        # Setup signal handlers
        loop = asyncio.get_event_loop()

        def handle_signal(sig):
            self._shutdown = True
            if self._process:
                self._process.terminate()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        try:
            # Report starting
            await self.report_status("running")

            # Start heartbeat task
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            # Prepare environment
            env = os.environ.copy()
            env.update(self.environment)

            # Execute command
            self._process = await asyncio.create_subprocess_shell(
                self.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=env,
            )

            # Stream output
            await asyncio.gather(
                self._stream_output(self._process.stdout, "stdout"),
                self._stream_output(self._process.stderr, "stderr"),
            )

            # Wait for completion
            try:
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
                await self.report_status("failed", exit_code=124, error="Timeout")
                return 124

            exit_code = self._process.returncode or 0

            # Report completion
            if exit_code == 0:
                await self.report_status("completed", exit_code=0)
            else:
                await self.report_status("failed", exit_code=exit_code)

            return exit_code

        except Exception as e:
            await self.report_status("failed", exit_code=1, error=str(e))
            return 1

        finally:
            # Stop heartbeat
            self._shutdown = True
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass


async def main():
    """Main entry point."""
    # Load config
    if not CONFIG_PATH.exists():
        print(f"[control] Config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    print(f"[control] Starting step: {config.get('step_id')}")
    print(f"[control] Command: {config.get('command')}")

    # Run control layer
    controller = ControlLayer(config)
    exit_code = await controller.run()

    print(f"[control] Step completed with exit code: {exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
