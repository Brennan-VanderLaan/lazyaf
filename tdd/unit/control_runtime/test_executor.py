"""
Unit tests for the control runtime's command executor
(images/base/control/executor.py).

Real subprocesses through bash (present on the Windows dev host via Git Bash
and in the Linux image); the backend client is a fake. Covers the audit
fixes: in-container timeout enforcement (exit 124) with poll-before-deadline
ordering, timer-based flush for quiet processes, capped flush slices with a
hard drop-oldest buffer ceiling, an HTTP-free watchdog loop, and
shell-wrapping with `set -e` + mkdir-HOME semantics identical to
local_executor.build_step_command.
"""
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import control.executor as executor_mod
from control.config import StepConfig
from control.executor import (
    TIMEOUT_EXIT_CODE,
    ExecutionResult,
    build_shell_command,
    execute_command,
)
from tdd.shared import wait


class FakeClient:
    def __init__(self):
        self.batches = []  # (monotonic_time, lines)
        self.dropped_log_lines = 0

    def send_logs(self, lines, stream="stdout"):
        self.batches.append((time.monotonic(), list(lines)))
        return True

    def all_lines(self):
        return [line for _, lines in self.batches for line in lines]


def _config(command, **overrides):
    defaults = dict(
        step_id="exec-1",
        backend_url="http://backend:8000",
        auth_token="tok",
        command=command,
        working_directory=".",
        timeout_seconds=30,
    )
    defaults.update(overrides)
    return StepConfig(**defaults)


@pytest.fixture
def fast_flush(monkeypatch):
    """Timer-driven flushes every 0.1s (the constant is a module knob now)."""
    monkeypatch.setattr(executor_mod, "LOG_BATCH_INTERVAL", 0.1)


class TestShellWrapping:
    def test_wraps_string_with_set_e_prelude(self):
        wrapped = build_shell_command("echo hi")
        # On Windows hosts the shell is a resolved absolute path (Git bash);
        # in the Linux image it is plain "bash".
        assert "bash" in wrapped[0].lower()
        assert wrapped[1:] == ["-c", "set -e\necho hi"]

    def test_shell_override(self):
        assert "sh" in build_shell_command("echo hi", shell="sh")[0].lower()

    def test_home_prelude_mkdirs_effective_home(self):
        """Same contract as the stdout-mode builder: the wrapper mkdir -p's
        the EFFECTIVE HOME before the user script runs."""
        wrapped = build_shell_command("echo hi", home="/workspace/home")
        assert wrapped[1:] == [
            "-c",
            "set -e\nmkdir -p /workspace/home\necho hi",
        ]

    def test_home_prelude_quotes_the_path(self):
        wrapped = build_shell_command("echo hi", home="/work space/ho me")
        assert "mkdir -p '/work space/ho me'\n" in wrapped[2]

    def test_no_home_no_mkdir(self):
        assert "mkdir" not in build_shell_command("echo hi")[2]

    def test_set_e_fails_at_first_failing_line(self, tmp_path):
        """A multiline script whose middle command dies must not read green."""
        client = FakeClient()
        config = _config(
            "echo before\nfalse\necho after",
            working_directory=str(tmp_path),
        )

        result = execute_command(config, client)

        assert result.exit_code != 0
        assert result.timed_out is False
        joined = "".join(client.all_lines())
        assert "before" in joined
        assert "after" not in joined

    def test_execute_creates_effective_home(self, tmp_path):
        """execute_command derives HOME from the merged environment and the
        prelude creates it — a fresh volume path must exist for the script."""
        home = str(tmp_path / "step-home").replace("\\", "/")
        client = FakeClient()
        config = _config(
            'echo "HOME=$HOME"',
            working_directory=str(tmp_path),
            environment={"HOME": home},
        )

        result = execute_command(config, client)

        assert result.exit_code == 0
        assert (tmp_path / "step-home").is_dir()
        # Git Bash rewrites drive-letter paths (/c/...) in $HOME on the
        # Windows dev host; assert on the distinctive tail, not the prefix.
        assert any(
            "HOME=" in line and "step-home" in line
            for line in client.all_lines()
        )

    def test_config_shell_is_used(self, tmp_path):
        """The shell comes from the config contract (no getattr default)."""
        client = FakeClient()
        config = _config(
            "echo via-sh", working_directory=str(tmp_path), shell="sh"
        )

        result = execute_command(config, client)

        assert result.exit_code == 0
        assert "via-sh\n" in client.all_lines()


class TestExecution:
    def test_captures_exit_code_and_logs(self, tmp_path):
        client = FakeClient()
        config = _config("echo one\necho two", working_directory=str(tmp_path))

        result = execute_command(config, client)

        assert result == ExecutionResult(exit_code=0, timed_out=False)
        assert client.all_lines() == ["one\n", "two\n"]

    def test_lines_keep_trailing_newlines(self, tmp_path):
        client = FakeClient()
        config = _config("printf 'a\\nb\\n'", working_directory=str(tmp_path))

        execute_command(config, client)

        for line in client.all_lines():
            assert line.endswith("\n")

    def test_stderr_merged_into_stdout_stream(self, tmp_path):
        client = FakeClient()
        config = _config("echo oops >&2", working_directory=str(tmp_path))

        result = execute_command(config, client)

        assert result.exit_code == 0
        assert "oops\n" in client.all_lines()

    def test_environment_merged(self, tmp_path):
        client = FakeClient()
        config = _config(
            'echo "VAL=$MYVAR"',
            working_directory=str(tmp_path),
            environment={"MYVAR": "hello-env"},
        )

        result = execute_command(config, client)

        assert result.exit_code == 0
        assert any("VAL=hello-env" in line for line in client.all_lines())

    def test_working_directory_respected(self, tmp_path):
        marker = tmp_path / "marker-file.txt"
        marker.write_text("x")
        client = FakeClient()
        config = _config("ls", working_directory=str(tmp_path))

        result = execute_command(config, client)

        assert result.exit_code == 0
        assert any("marker-file.txt" in line for line in client.all_lines())

    def test_nonzero_exit_code_propagated(self, tmp_path):
        client = FakeClient()
        config = _config("exit 7", working_directory=str(tmp_path))

        result = execute_command(config, client)

        assert result.exit_code == 7
        assert result.timed_out is False


class TestQuietProcessFlush:
    def test_flushes_on_timer_without_new_lines(self, tmp_path, fast_flush):
        """failure_01 only flushed when a NEW line arrived — a quiet process
        kept its earlier lines hostage. The flush must fire on the interval."""
        client = FakeClient()
        config = _config("echo early\nsleep 0.6", working_directory=str(tmp_path))

        start = time.monotonic()
        result = execute_command(config, client)
        total = time.monotonic() - start

        assert result.exit_code == 0
        assert total >= 0.5  # the sleep really happened
        assert client.batches, "no logs sent at all"
        first_batch_at = client.batches[0][0] - start
        assert first_batch_at < 0.5, (
            f"first batch arrived after {first_batch_at:.2f}s — flushed only "
            "at process exit, not on the timer"
        )
        assert "early\n" in client.batches[0][1]

    def test_flushes_on_batch_size(self, tmp_path, monkeypatch):
        """The size trigger flushes while the process is STILL RUNNING - not
        the final drain wearing a size-capped slice as a disguise.

        No clock, and no process the test spawns on its own exit path. The
        script prints two batches' worth of lines and then parks on the
        bash BUILTIN `read` against a stdin pipe the test owns. The client
        observes the product signal directly: on its first `send_logs` it
        records whether the process is still alive (`proc.poll() is None`)
        and only then closes stdin, which is what lets bash exit. The only
        other flush path is the final drain, which starts after
        `process.wait()` (executor.py:274-275), so a flush that saw the
        process alive can only have been the size trigger (executor.py:214).

        Two earlier forms lost on a loaded host without the product being
        wrong. `batches[0] - start < 0.45 s` with `start` taken before Popen
        measured bash spawn time. Its replacement had the script poll for a
        file with `while [ ! -e flushed ]; do sleep 0.02; done` - a `sleep`
        PROCESS per iteration - and under a saturated host the size trigger
        fired while the process was alive in 10/10 probe runs, yet 2/10
        ended in the watchdog's exit 124 (one with MSYS `sleep` dying on
        `cygheap read copy failed`) because the exit path, not the trigger,
        was starved; the assertion then blamed the trigger. `read` spawns
        nothing, and the liveness flag is the claim itself.
        """
        monkeypatch.setattr(executor_mod, "LOG_BATCH_SIZE", 2)
        monkeypatch.setattr(executor_mod, "LOG_BATCH_INTERVAL", 30.0)  # timer off

        # Capture the Popen so the client can poll it, and give bash a stdin
        # pipe for `read` to park on. Scoped to the executor module (same
        # shape as the `threading` namespace in test_failed_final_drain_...):
        # the proxy forwards everything else - PIPE, STDOUT, run,
        # TimeoutExpired - to the real module untouched.
        captured: list[subprocess.Popen] = []

        class CapturingPopen(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                kwargs["stdin"] = subprocess.PIPE
                super().__init__(*args, **kwargs)
                captured.append(self)

        class _Subprocess:
            Popen = CapturingPopen

            def __getattr__(self, name):
                return getattr(subprocess, name)

        monkeypatch.setattr(executor_mod, "subprocess", _Subprocess())

        alive: dict[str, bool] = {}

        class ReleasingClient(FakeClient):
            def send_logs(self, lines, stream="stdout"):
                ok = super().send_logs(lines, stream)
                if "at_first_flush" not in alive:
                    # The sender thread starts after Popen returned
                    # (executor.py:262-265), so the process is captured.
                    (proc,) = captured
                    alive["at_first_flush"] = proc.poll() is None
                    proc.stdin.close()  # EOF for `read`: the process's exit
                return ok

        client = ReleasingClient()
        config = _config(
            "for i in 1 2 3 4; do echo line$i; done\n"
            "read -r _ || true",  # `set -e`: EOF must not become exit 1
            working_directory=str(tmp_path),
            # Only the bound on a BROKEN trigger: a green run exits on the
            # stdin close and never comes near it, so it is wide on purpose
            # (three times the shared default) - the probe above saw bash's
            # first echo land 11 s and 88 s after Popen on the saturated
            # host, and a bound this test can trip for host reasons is the
            # defect it was rewritten to remove. A trigger that never fires
            # ends here as a watchdog kill (exit 124) instead of hanging
            # the tier on `read`.
            timeout_seconds=3 * wait.DEFAULT_TIMEOUT,
        )

        result = execute_command(config, client)

        # The claim, observed where it happens: the first flush found the
        # process alive. A missing key means nothing was ever sent while it
        # ran - the watchdog killed it and even the final drain sent nothing.
        assert alive.get("at_first_flush") is True, (
            "the size trigger never flushed while the process was alive: "
            f"first flush saw alive={alive.get('at_first_flush')!r}, "
            f"result={result!r}"
        )
        # And it was the SIZE that triggered it: one exact LOG_BATCH_SIZE
        # slice, the first two lines in order (executor.py:214).
        assert client.batches[0][1] == ["line1\n", "line2\n"]
        assert sorted(client.all_lines()) == [
            "line1\n", "line2\n", "line3\n", "line4\n",
        ]
        # The exit was the test's own stdin close, not the watchdog: bash
        # read EOF and left through `|| true`, so the result is a clean 0.
        assert result.timed_out is False
        assert result.exit_code == 0


class TestFlushDiscipline:
    def test_flush_slices_capped_at_batch_size(self, tmp_path, monkeypatch):
        """A backlog drains in LOG_BATCH_SIZE slices — never one giant POST."""
        monkeypatch.setattr(executor_mod, "LOG_BATCH_SIZE", 2)
        monkeypatch.setattr(executor_mod, "LOG_BATCH_INTERVAL", 30.0)
        client = FakeClient()
        config = _config(
            "for i in 1 2 3 4 5; do echo line$i; done",
            working_directory=str(tmp_path),
        )

        result = execute_command(config, client)

        assert result.exit_code == 0
        assert sorted(client.all_lines()) == [
            f"line{i}\n" for i in range(1, 6)
        ]
        for _, lines in client.batches:
            assert len(lines) <= 2, lines

    def test_buffer_ceiling_drops_oldest_with_counted_marker(
        self, tmp_path, monkeypatch
    ):
        """Beyond MAX_BUFFER_LINES the OLDEST lines drop, the count surfaces
        as a '[control] N lines dropped locally' marker line."""
        monkeypatch.setattr(executor_mod, "MAX_BUFFER_LINES", 10)
        monkeypatch.setattr(executor_mod, "LOG_BATCH_SIZE", 100)
        monkeypatch.setattr(executor_mod, "LOG_BATCH_INTERVAL", 30.0)
        client = FakeClient()
        config = _config(
            "for i in $(seq 1 50); do echo line$i; done",
            working_directory=str(tmp_path),
        )

        result = execute_command(config, client)

        assert result.exit_code == 0
        lines = client.all_lines()
        assert "[control] 40 lines dropped locally\n" in lines
        assert "line50\n" in lines  # newest survived
        assert "line1\n" not in lines  # oldest dropped
        assert len([l for l in lines if l.startswith("line")]) == 10

    def test_watchdog_not_blocked_by_slow_log_http(self, tmp_path, monkeypatch):
        """The timeout kill must land on schedule even when every send_logs
        call is slow — the watchdog loop owns no HTTP."""
        monkeypatch.setattr(executor_mod, "LOG_BATCH_INTERVAL", 0.05)

        class SlowClient(FakeClient):
            def send_logs(self, lines, stream="stdout"):
                time.sleep(0.5)
                return super().send_logs(lines, stream)

        client = SlowClient()
        config = _config(
            "echo tick\nsleep 30",
            working_directory=str(tmp_path),
            timeout_seconds=0.4,
        )

        start = time.monotonic()
        result = execute_command(config, client)
        total = time.monotonic() - start

        assert result.timed_out is True
        assert result.exit_code == TIMEOUT_EXIT_CODE
        assert total < 10, "slow log HTTP delayed the watchdog kill"
        assert "tick\n" in client.all_lines()

    def test_failed_final_drain_counts_remainder_as_dropped(
        self, tmp_path, monkeypatch
    ):
        """When the backend refuses logs at shutdown, the drain bails after
        the first failed slice instead of stalling slice-by-slice behind
        retry budgets — everything left is counted as dropped."""
        monkeypatch.setattr(executor_mod, "LOG_BATCH_SIZE", 2)
        monkeypatch.setattr(executor_mod, "LOG_BATCH_INTERVAL", 30.0)

        class GatedRefusingClient(FakeClient):
            """First send blocks until released AFTER the process exited, so
            the remaining slices are guaranteed to hit the stopping path."""

            def __init__(self):
                super().__init__()
                self.gate = threading.Event()

            def send_logs(self, lines, stream="stdout"):
                # The deadline of an event wait, not its schedule: the gate
                # is set by `sender_stop` below. Left unset, the send returns
                # and the batch count below names the defect.
                self.gate.wait(timeout=wait.DEFAULT_TIMEOUT)
                super().send_logs(lines, stream)
                self.dropped_log_lines += len(lines)  # what a real client does
                return False

        client = GatedRefusingClient()

        # WHAT OPENS THE GATE. The blocked first slice must return only after
        # `sender_stop` is set (executor.py:275, right after `process.wait()`).
        # Released any earlier it is a pre-stop slice, the next slice is
        # `due` pre-stop as well (executor.py:214), and the bail path this
        # test exists for is never reached - three batches instead of two.
        # A `threading.Timer(0.8, ...)` guessed that five echos exit inside
        # 0.8 s; a loaded host disagreed. So the gate opens ON that set:
        # executor.py:167-168 creates `output_done` then `sender_stop`, and
        # the executor reaches `threading` through its module global, so a
        # namespace handing out one instrumented Event per call is scoped to
        # this one execute_command and touches no other thread's Events.
        events: list[threading.Event] = []

        class _Event(threading.Event):
            def set(self):
                super().set()
                if events.index(self) == 1:  # sender_stop
                    client.gate.set()

        def _event():
            event = _Event()
            events.append(event)
            return event

        monkeypatch.setattr(
            executor_mod,
            "threading",
            SimpleNamespace(
                Event=_event, Lock=threading.Lock, Thread=threading.Thread
            ),
        )
        config = _config(
            "for i in 1 2 3 4 5; do echo line$i; done",
            working_directory=str(tmp_path),
        )

        try:
            result = execute_command(config, client)
        finally:
            client.gate.set()

        assert result.exit_code == 0
        # At most one pre-stop slice plus ONE stopping slice before the bail
        # (never slice-by-slice retries through the whole backlog), and every
        # line is accounted for: attempted-and-counted by the client, or
        # counted by the bail.
        assert len(client.batches) <= 2
        assert client.dropped_log_lines == 5


class TestTimeoutEnforcement:
    def test_timeout_kills_and_reports_124(self, tmp_path):
        """timeout_seconds was loaded but NEVER enforced on failure_01."""
        client = FakeClient()
        config = _config(
            "echo started\nsleep 30",
            working_directory=str(tmp_path),
            timeout_seconds=0.4,
        )

        start = time.monotonic()
        result = execute_command(config, client)
        total = time.monotonic() - start

        assert result.timed_out is True
        assert result.exit_code == TIMEOUT_EXIT_CODE
        assert total < 10, "timeout kill did not take effect promptly"
        assert "started\n" in client.all_lines()

    def test_fast_process_does_not_time_out(self, tmp_path):
        client = FakeClient()
        config = _config("echo quick", working_directory=str(tmp_path), timeout_seconds=5)

        result = execute_command(config, client)

        assert result.timed_out is False
        assert result.exit_code == 0

    def test_exit_during_sleep_at_deadline_is_not_a_timeout(
        self, tmp_path, monkeypatch
    ):
        """WATCHDOG ORDER boundary: the process exits while the loop sleeps
        AND the deadline passes during that same sleep. poll() must win —
        deadline-first ordering falsely reported exit 124 here."""
        real_time = time

        class SlowTickTime:
            """Real clock, but each watchdog tick sleeps 1s — guaranteeing
            the process exit and the deadline both land inside one tick."""

            monotonic = staticmethod(real_time.monotonic)

            @staticmethod
            def sleep(_seconds):
                real_time.sleep(1.0)

        monkeypatch.setattr(executor_mod, "time", SlowTickTime)

        client = FakeClient()
        config = _config(
            "sleep 0.1",
            working_directory=str(tmp_path),
            timeout_seconds=0.5,
        )

        result = execute_command(config, client)

        assert result == ExecutionResult(exit_code=0, timed_out=False)
