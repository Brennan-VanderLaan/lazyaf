"""
Command Executor.

Executes step commands, captures output, and streams logs to backend.
"""
import os
import subprocess
import time
from typing import List


def execute_command(config, client) -> int:
    """
    Execute the step command, streaming logs to backend.

    Args:
        config: StepConfig with command, working_dir, environment, etc.
        client: BackendClient for sending logs

    Returns:
        Exit code from the process
    """
    # Merge environment
    env = os.environ.copy()
    env.update(config.environment)

    # Start process
    process = subprocess.Popen(
        config.command,
        cwd=config.working_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # Line buffered
    )

    log_buffer: List[str] = []
    last_flush = time.time()

    def flush_logs():
        """Send buffered logs to backend."""
        nonlocal log_buffer, last_flush
        if log_buffer:
            client.send_logs(log_buffer)
            log_buffer = []
            last_flush = time.time()

    # Stream output
    try:
        for line in process.stdout:
            # Echo to local stdout (for Docker log streaming)
            print(line, end="", flush=True)

            # Buffer for backend
            log_buffer.append(line.rstrip("\n\r"))

            # Flush on batch size or time interval
            if (
                len(log_buffer) >= config.log_batch_size
                or time.time() - last_flush >= config.log_batch_interval
            ):
                flush_logs()

    except Exception as e:
        log_buffer.append(f"[control] Error reading output: {e}")

    # Wait for process to complete
    process.wait()

    # Final flush
    flush_logs()

    return process.returncode
