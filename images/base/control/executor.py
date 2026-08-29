"""
Command Executor.

Executes the step command, captures output, streams logs to backend, and
ENFORCES the step timeout in-container (graceful SIGTERM -> grace -> SIGKILL).
The backend executor's hard deadline (timeout + grace) remains the backstop.

Semantics mirror the backend's ``local_executor.build_step_command``: the raw
command STRING is shell-wrapped as ``[shell, "-c", "set -e\\n" + mkdir-HOME +
command]`` so scripts behave identically in stdout mode and control mode — a
multiline script whose middle command dies fails at that line, not at the last
one, and $HOME exists on the shared volume before the user script runs.

Threading model (audit fix — the watchdog must never block on HTTP):
- log-reader thread: drains the process pipe into a bounded buffer
- log-sender thread: owns ALL ``client.send_logs`` calls (slices capped at
  LOG_BATCH_SIZE, timer-driven flushes every LOG_BATCH_INTERVAL)
- main (watchdog) loop: only polls the process and the deadline
"""
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Exit code reported when the in-container watchdog kills the step
# (matches coreutils `timeout`).
TIMEOUT_EXIT_CODE = 124

# Grace between SIGTERM and SIGKILL on timeout.
SIGTERM_GRACE_SECONDS = 5.0

# Main loop tick.
_POLL_INTERVAL = 0.05

# Log shipping knobs — module constants, intentionally NOT part of the step
# config wire contract (see tdd/unit/control_runtime/test_config_contract.py).
LOG_BATCH_SIZE = 100  # max lines per /logs POST (flush slices are capped here)
LOG_BATCH_INTERVAL = 1.0  # seconds between timer-driven flushes
MAX_BUFFER_LINES = 5000  # hard local ceiling; beyond it the OLDEST lines drop


@dataclass
class ExecutionResult:
    """Outcome of a step command."""

    exit_code: int
    timed_out: bool = False


def build_shell_command(
    command: str, shell: str = "bash", home: Optional[str] = None
) -> List[str]:
    """Shell-wrap the raw command string with a ``set -e`` prelude.

    When ``home`` is given, the prelude also ``mkdir -p``'s that EFFECTIVE
    HOME (quoted) — the same guarantee the stdout-mode builder
    (``local_executor.build_step_command``) provides, so user scripts start
    with a writable $HOME on the shared workspace volume in both modes.

    On non-POSIX hosts (unit tests on the Windows dev box; the image itself
    is Linux) the shell is resolved through PATH via shutil.which — otherwise
    CreateProcess finds System32 first and ``bash`` silently becomes WSL's
    launcher, which mangles ``$`` expansion and drops the environment.
    """
    if os.name != "posix":
        shell = shutil.which(shell) or shell
    prelude = "set -e\n"
    if home:
        prelude += f"mkdir -p {shlex.quote(home)}\n"
    return [shell, "-c", prelude + command]


def _kill_tree_windows(process: "subprocess.Popen") -> None:
    """Kill the whole process tree on Windows (unit-test hosts only).

    process.terminate() would kill only the shell; a grandchild (e.g. sleep)
    keeps the stdout pipe open and the log reader blocks until it exits.
    """
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        capture_output=True,
    )


def _terminate(process: "subprocess.Popen") -> None:
    """SIGTERM the whole process group (POSIX) or kill the tree (elsewhere)."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:  # pragma: no cover - unit-test hosts only; image is Linux
            _kill_tree_windows(process)
    except (ProcessLookupError, OSError):
        pass


def _kill(process: "subprocess.Popen") -> None:
    """SIGKILL the whole process group (POSIX) or kill the tree (elsewhere)."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover
            _kill_tree_windows(process)
    except (ProcessLookupError, OSError):
        pass


def _kill_with_grace(process: "subprocess.Popen") -> None:
    """Graceful stop: SIGTERM, wait SIGTERM_GRACE_SECONDS, then SIGKILL."""
    _terminate(process)
    try:
        process.wait(timeout=SIGTERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _kill(process)


def execute_command(config, client) -> ExecutionResult:
    """
    Execute the step command, streaming logs to backend.

    - A dedicated sender thread owns all log HTTP: flush slices are capped at
      LOG_BATCH_SIZE lines, fire on size or every LOG_BATCH_INTERVAL, and a
      hard MAX_BUFFER_LINES ceiling drops the OLDEST lines (counted and
      surfaced as a ``[control] N lines dropped locally`` marker line).
    - The watchdog loop never blocks on HTTP, and it polls the process BEFORE
      the deadline check — a process that exited during the sleep is a normal
      exit, never a timeout.
    - ``timeout_seconds`` is enforced here: on deadline the process group is
      SIGTERMed, then SIGKILLed after a grace period; the result reports
      exit code 124 and ``timed_out=True``.

    Args:
        config: StepConfig with command, shell, working_directory, etc.
        client: BackendClient for sending logs

    Returns:
        ExecutionResult with exit code and timeout flag
    """
    env = os.environ.copy()
    env.update(config.environment)

    popen_kwargs = {}
    if os.name == "posix":
        # Own session/process group so the timeout watchdog can kill the
        # whole tree, not just the shell.
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        build_shell_command(config.command, config.shell, home=env.get("HOME")),
        cwd=config.working_directory,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # Line buffered
        **popen_kwargs,
    )

    buffer: deque = deque()
    dropped_locally = 0
    buffer_lock = threading.Lock()
    output_done = threading.Event()
    sender_stop = threading.Event()

    def _read_output() -> None:
        nonlocal dropped_locally
        try:
            for line in process.stdout:
                # Echo to local stdout (docker-logs forensics in both modes)
                print(line, end="", flush=True)
                with buffer_lock:
                    if len(buffer) >= MAX_BUFFER_LINES:
                        buffer.popleft()  # drop-oldest under the hard ceiling
                        dropped_locally += 1
                    buffer.append(line)
        except Exception as e:  # pragma: no cover - defensive
            with buffer_lock:
                buffer.append(f"[control] Error reading output: {e}\n")
        finally:
            output_done.set()

    def _drain_slice() -> Tuple[bool, bool]:
        """Send at most one LOG_BATCH_SIZE slice. -> (sent_anything, send_ok)."""
        nonlocal dropped_locally
        with buffer_lock:
            marker = None
            if dropped_locally:
                marker = f"[control] {dropped_locally} lines dropped locally\n"
                dropped_locally = 0
            limit = LOG_BATCH_SIZE - (1 if marker else 0)
            batch: List[str] = []
            while buffer and len(batch) < limit:
                batch.append(buffer.popleft())
        lines = ([marker] if marker else []) + batch
        if not lines:
            return (False, True)
        return (True, client.send_logs(lines))

    def _sender_loop() -> None:
        """Owns all log HTTP so the watchdog loop never blocks on it."""
        nonlocal dropped_locally
        tick = min(_POLL_INTERVAL, float(LOG_BATCH_INTERVAL))
        last_send = time.monotonic()
        while True:
            stopping = sender_stop.is_set()
            with buffer_lock:
                pending = len(buffer)
                pending_marker = dropped_locally > 0
            due = pending >= LOG_BATCH_SIZE or (
                (pending or pending_marker)
                and time.monotonic() - last_send >= float(LOG_BATCH_INTERVAL)
            )
            if stopping or due:
                sent, ok = _drain_slice()
                if sent:
                    last_send = time.monotonic()
                if stopping:
                    if not sent:
                        break  # fully drained
                    if not ok:
                        # Backend is refusing logs; do not stall shutdown
                        # behind per-slice retry budgets — count the rest as
                        # dropped (run.py surfaces the total in the final
                        # status) and bail.
                        with buffer_lock:
                            remaining = len(buffer) + dropped_locally
                            buffer.clear()
                            dropped_locally = 0
                        if remaining:
                            client.dropped_log_lines += remaining
                        break
                    continue  # keep draining slices back-to-back
            if not stopping:
                sender_stop.wait(timeout=tick)

    reader = threading.Thread(target=_read_output, daemon=True, name="log-reader")
    sender = threading.Thread(target=_sender_loop, daemon=True, name="log-sender")
    reader.start()
    sender.start()

    deadline = time.monotonic() + float(config.timeout_seconds)
    timed_out = False

    # Watchdog loop. Ordering matters: poll the process FIRST — a process
    # that exited while we slept must be reported as a normal exit even if
    # the deadline also passed during that sleep.
    while True:
        if process.poll() is not None:
            # Bounded drain: normally the reader finishes moments after the
            # process; if an orphaned grandchild still holds the pipe open,
            # do not hang on it forever.
            output_done.wait(timeout=5.0)
            break

        if not timed_out and time.monotonic() >= deadline:
            timed_out = True
            # Kill in a side thread so log flushing continues through the
            # SIGTERM grace window.
            threading.Thread(
                target=_kill_with_grace,
                args=(process,),
                daemon=True,
                name="timeout-watchdog",
            ).start()

        time.sleep(_POLL_INTERVAL)

    reader.join(timeout=5.0)
    process.wait()
    sender_stop.set()  # final drain (slices, bail-on-failure) then exit
    sender.join(timeout=60.0)

    if timed_out:
        return ExecutionResult(exit_code=TIMEOUT_EXIT_CODE, timed_out=True)
    return ExecutionResult(exit_code=process.returncode, timed_out=False)
