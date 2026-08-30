"""
Tests for the agent step runtime (Phase 12.5).

`runner_common.agent_wrapper` is what the backend's fixed agent-step command
`python3 -m runner_common.agent_wrapper` runs inside a control-mode
container. Its contract with the rest of the system is small and load-bearing:

- its EXIT CODE is the step's exit code,
- its STDOUT is the step's log stream,
- it writes the usage manifest on EVERY outcome (contract #2), including
  when the executor raises and when the watchdog SIGTERMs it,
- it never runs as root and never guesses an agent.

Everything here drives the REAL main() with real files; only the agent
executors are substituted, and the mock agent is exercised for real.
"""
import json
import signal
import subprocess

import pytest

from runner_common import agent_wrapper
from runner_common.agent_config import AGENT_CONFIG_PATH_ENV
from runner_common.executors import ExecutorResult
from runner_common.usage import USAGE_PATH_ENV


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class RecordingExecutor:
    """Stand-in that records how it was constructed and invoked."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        RecordingExecutor.last = self

    def execute(self, config, log_callback=None, streaming=True):
        self.calls.append({"config": config, "streaming": streaming})
        if log_callback:
            log_callback("  hello from the CLI")
        return ExecutorResult(
            success=True,
            exit_code=0,
            stdout="",
            usage={"provider": "anthropic", "cost_source": "cli-reported"},
        )


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """Write an agent config + point the wrapper at it. Returns a helper."""

    class Harness:
        def __init__(self):
            self.config_path = tmp_path / "agent.exec-1.json"
            self.usage_path = tmp_path / "usage.exec-1.json"
            self.workdir = tmp_path / "repo"
            self.workdir.mkdir()

        def write(self, **overrides):
            data = {
                "version": 1,
                "agent": "mock",
                "model": "claude-haiku-4-5",
                "stream": True,
                "prompt": "Implement the feature." * 10,
                "agents_json": None,
                "task": {"card_title": "Add rate limiting", "step_index": 3},
                "context": {},
                "repo": {"repo_id": "r9", "workdir": str(self.workdir)},
                "commit": {"enabled": False},
                "mock_config": {
                    "response_mode": "batch",
                    "delay_ms": 0,
                    "file_operations": [],
                    "output_events": [{"type": "complete", "text": "Done."}],
                    "exit_code": 0,
                },
                "role": None,
            }
            data.update(overrides)
            self.config_path.write_text(json.dumps(data), encoding="utf-8")
            monkeypatch.setenv(AGENT_CONFIG_PATH_ENV, str(self.config_path))
            monkeypatch.setenv(USAGE_PATH_ENV, str(self.usage_path))
            return self

        def run(self, **overrides):
            self.write(**overrides)
            return agent_wrapper.main()

        @property
        def manifest(self):
            if not self.usage_path.exists():
                return None
            return json.loads(self.usage_path.read_text(encoding="utf-8"))

    return Harness()


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

class TestDispatch:
    def test_mock_agent_runs_the_real_mock_executor(self, agent_env):
        exit_code = agent_env.run(
            mock_config={
                "delay_ms": 0,
                "file_operations": [
                    {
                        "action": "create",
                        "path": ".lazyaf-dogfood/agent-ran",
                        "content": "12.5 mock agent step\n",
                    }
                ],
                "output_events": [{"type": "complete", "text": "Done."}],
                "exit_code": 0,
            }
        )

        assert exit_code == 0
        landed = agent_env.workdir / ".lazyaf-dogfood" / "agent-ran"
        assert landed.read_text() == "12.5 mock agent step\n"
        assert agent_env.manifest["provider"] == "self-hosted"
        assert agent_env.manifest["model"] == "mock"

    def test_claude_agent_gets_stream_json_when_stream_is_true(
        self, agent_env, monkeypatch
    ):
        monkeypatch.setattr(agent_wrapper, "ClaudeExecutor", RecordingExecutor)
        assert agent_env.run(agent="claude-code", stream=True) == 0
        assert RecordingExecutor.last.kwargs == {"output_format": "stream-json"}
        assert RecordingExecutor.last.calls[0]["streaming"] is True

    def test_claude_agent_falls_back_to_json_when_stream_is_false(
        self, agent_env, monkeypatch
    ):
        monkeypatch.setattr(agent_wrapper, "ClaudeExecutor", RecordingExecutor)
        assert agent_env.run(agent="claude-code", stream=False) == 0
        assert RecordingExecutor.last.kwargs == {"output_format": "json"}
        assert RecordingExecutor.last.calls[0]["streaming"] is False

    def test_gemini_agent_selects_the_gemini_executor(self, agent_env, monkeypatch):
        monkeypatch.setattr(agent_wrapper, "GeminiExecutor", RecordingExecutor)
        assert agent_env.run(agent="gemini") == 0
        assert RecordingExecutor.last.kwargs == {}

    def test_executor_config_carries_the_agent_config_fields(
        self, agent_env, monkeypatch
    ):
        monkeypatch.setattr(agent_wrapper, "ClaudeExecutor", RecordingExecutor)
        agent_env.run(agent="claude-code", agents_json='{"fixer": {}}')

        config = RecordingExecutor.last.calls[0]["config"]
        assert config.prompt.startswith("Implement the feature.")
        assert config.model == "claude-haiku-4-5"
        assert config.agents_json == '{"fixer": {}}'
        assert str(config.workspace) == str(agent_env.workdir)

    def test_timeout_is_none_by_contract(self, agent_env, monkeypatch):
        """ONE timeout owner: the control runtime's watchdog. Two owners is
        how a step ends up half-killed with no manifest."""
        monkeypatch.setattr(agent_wrapper, "ClaudeExecutor", RecordingExecutor)
        agent_env.run(agent="claude-code")
        assert RecordingExecutor.last.calls[0]["config"].timeout is None

    def test_unknown_agent_exits_one_and_names_the_vocabulary(
        self, agent_env, capsys
    ):
        assert agent_env.run(agent="gpt-5") == 1
        err = capsys.readouterr().err
        assert "unknown agent 'gpt-5'" in err
        assert "no default agent" in err

    def test_unknown_agent_still_writes_a_usage_manifest(self, agent_env):
        """F3.3: the refusal moved INSIDE the try whose finally writes the
        manifest. EVERY control-mode step produces a row, and the dogfood
        gate asserts that completeness - a step that dies on dispatch used
        to leave a hole in it."""
        assert agent_env.run(agent="gpt-5") == 1
        manifest = agent_env.manifest
        assert manifest is not None
        assert manifest["cost_source"] == "unknown"
        # An agent outside the vocabulary has no provider mapping.
        assert manifest["provider"] == "self-hosted"

    def test_a_failing_executor_constructor_still_writes_a_manifest(
        self, agent_env, monkeypatch, capsys
    ):
        """The construction moved inside the try too: an executor whose
        __init__ raises is the same hole with a different cause."""

        def exploding_builder(cfg):
            raise RuntimeError("CLI binary is missing")

        monkeypatch.setitem(
            agent_wrapper.EXECUTORS, "claude-code", exploding_builder
        )
        assert agent_env.run(agent="claude-code") == 1
        assert "CLI binary is missing" in capsys.readouterr().err
        assert agent_env.manifest is not None

    def test_make_executor_and_the_vocabulary_have_one_source(self):
        """F3.3: EXECUTORS is the ONLY mapping. An agent it does not name
        cannot be constructed, and one it names must be."""
        # 14.2 added the FOURTH and only new entry (cross-agent contract #5).
        assert sorted(agent_wrapper.EXECUTORS) == [
            "claude-code",
            "gemini",
            "mock",
            "openai-harness",
        ]
        with pytest.raises(KeyError):
            agent_wrapper.make_executor(
                type("Cfg", (), {"agent": "gpt-5", "stream": True, "mock_config": None})
            )


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------

class TestRefusals:
    def test_running_as_root_exits_one(self, agent_env, monkeypatch, capsys):
        monkeypatch.setattr(agent_wrapper, "_running_as_root", lambda: True)
        assert agent_env.run() == 1
        err = capsys.readouterr().err
        assert "running as root" in err
        assert "gosu down to uid 1000" in err

    def test_running_as_root_predicate_is_false_on_this_host(self):
        """The predicate itself, not a stub: a developer machine and a
        gosu'd container both answer False."""
        assert agent_wrapper._running_as_root() is False

    def test_missing_config_path_env_exits_one(self, monkeypatch, capsys):
        monkeypatch.delenv(AGENT_CONFIG_PATH_ENV, raising=False)
        assert agent_wrapper.main() == 1
        assert AGENT_CONFIG_PATH_ENV in capsys.readouterr().err

    def test_unreadable_config_exits_one(self, agent_env, monkeypatch, capsys):
        agent_env.config_path.write_text("{broken", encoding="utf-8")
        monkeypatch.setenv(AGENT_CONFIG_PATH_ENV, str(agent_env.config_path))
        monkeypatch.setenv(USAGE_PATH_ENV, str(agent_env.usage_path))
        assert agent_wrapper.main() == 1
        assert "not valid JSON" in capsys.readouterr().err

    def test_config_is_consumed_once(self, agent_env):
        agent_env.run()
        assert not agent_env.config_path.exists()

    def test_failing_agent_propagates_its_exit_code(self, agent_env):
        assert agent_env.run(
            mock_config={"exit_code": 7, "output_events": [], "delay_ms": 0}
        ) == 7


# --------------------------------------------------------------------------
# usage manifest: written on every outcome
# --------------------------------------------------------------------------

class TestUsageManifestAlways:
    def test_written_on_success(self, agent_env):
        agent_env.run()
        assert agent_env.manifest["cost_source"] == "cli-reported"
        assert agent_env.manifest["input_tokens"] > 0

    def test_written_when_the_agent_fails(self, agent_env):
        agent_env.run(mock_config={"exit_code": 3, "output_events": [], "delay_ms": 0})
        assert agent_env.manifest is not None

    def test_written_in_a_finally_when_the_executor_raises(
        self, agent_env, monkeypatch, capsys
    ):
        class Exploding:
            def execute(self, *args, **kwargs):
                raise RuntimeError("CLI vanished")

        monkeypatch.setattr(agent_wrapper, "make_executor", lambda cfg: Exploding())

        assert agent_env.run(agent="claude-code") == 1
        assert "agent execution crashed" in capsys.readouterr().err
        manifest = agent_env.manifest
        assert manifest is not None
        # Nothing was reported, so the record says exactly that.
        assert manifest["cost_source"] == "unknown"
        assert manifest["cost_usd"] is None
        assert manifest["input_tokens"] is None
        assert manifest["provider"] == "anthropic"

    def test_wrapper_owned_wall_clock_is_present(self, agent_env):
        agent_env.run()
        assert isinstance(agent_env.manifest["wall_clock_ms"], int)

    def test_run_py_owned_fields_are_left_null(self, agent_env):
        """R3, one writer per datum: run.py owns timing and node
        attribution because it is the only component present for script
        steps too."""
        agent_env.run()
        manifest = agent_env.manifest
        assert manifest["container_seconds"] is None
        assert manifest["gpu_node_id"] is None
        assert manifest["gpu_fraction"] is None

    def test_no_usage_path_is_not_an_error(self, agent_env, monkeypatch):
        agent_env.write()
        monkeypatch.delenv(USAGE_PATH_ENV, raising=False)
        assert agent_wrapper.main() == 0
        assert agent_env.manifest is None

    def test_sigterm_handler_writes_a_partial_manifest_and_exits_143(
        self, agent_env, monkeypatch
    ):
        """A graceful watchdog kill (SIGTERM -> 5s -> SIGKILL) must still
        yield telemetry."""
        previous = signal.getsignal(signal.SIGTERM)
        try:
            agent_env.write()
            agent_wrapper._STATE.update(
                {
                    "usage_path": str(agent_env.usage_path),
                    "agent": "claude-code",
                    "model": "claude-haiku-4-5",
                    "role": None,
                    "started": 0.0,
                    "written": False,
                }
            )
            agent_wrapper._install_sigterm_handler()
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)

            with pytest.raises(SystemExit) as exc:
                handler(signal.SIGTERM, None)
        finally:
            signal.signal(signal.SIGTERM, previous)

        assert exc.value.code == 128 + signal.SIGTERM
        manifest = agent_env.manifest
        assert manifest["cost_source"] == "unknown"
        assert manifest["provider"] == "anthropic"
        assert manifest["model"] == "claude-haiku-4-5"

    def test_a_scrape_failure_is_announced_on_the_step_log_stream(
        self, agent_env, monkeypatch, capsys
    ):
        """F3.1: a vendor output change must be LOUD.

        The executor here behaves exactly like a claude CLI whose result
        object moved: it succeeds, and its usage block is a failed scrape.
        The wrapper prints the stable marker `scripts/verify_executor.py`
        greps for - and STILL exits 0, because telemetry never fails a step.
        """
        from runner_common.usage import (
            RAW_SCRAPE_FAILED,
            SCRAPE_FAILED_LOG_MARKER,
            scrape_claude_usage,
        )

        class VendorChanged:
            def execute(self, config, log_callback=None, streaming=True):
                return ExecutorResult(
                    success=True,
                    exit_code=0,
                    stdout="",
                    usage=scrape_claude_usage("the CLI printed prose now"),
                )

        monkeypatch.setitem(
            agent_wrapper.EXECUTORS, "claude-code", lambda cfg: VendorChanged()
        )

        assert agent_env.run(agent="claude-code") == 0
        out = capsys.readouterr().out
        assert SCRAPE_FAILED_LOG_MARKER in out
        assert "NOT evidence that the step was free" in out
        assert agent_env.manifest["raw"][RAW_SCRAPE_FAILED] is True

    def test_a_healthy_run_says_nothing_about_scraping(self, agent_env, capsys):
        """The marker must mean something: a normal step never prints it."""
        from runner_common.usage import SCRAPE_FAILED_LOG_MARKER

        assert agent_env.run() == 0
        assert SCRAPE_FAILED_LOG_MARKER not in capsys.readouterr().out

    def test_a_killed_step_is_not_blamed_on_the_vendor(self, agent_env, capsys):
        """usage=None is "nothing was reported", not "we could not read what
        was reported". Only a scraper may claim a scrape failure."""
        from runner_common.usage import SCRAPE_FAILED_LOG_MARKER

        agent_env.write()
        agent_wrapper._STATE.update(
            {
                "usage_path": str(agent_env.usage_path),
                "agent": "claude-code",
                "model": None,
                "role": None,
                "started": 0.0,
                "written": False,
            }
        )
        agent_wrapper._write_usage(None)

        assert SCRAPE_FAILED_LOG_MARKER not in capsys.readouterr().out
        assert agent_env.manifest["raw"] is None

    def test_the_first_write_wins(self, agent_env):
        """The SIGTERM handler's partial record is not overwritten by the
        identical one the finally would produce on the way out."""
        agent_env.write()
        agent_wrapper._STATE.update(
            {
                "usage_path": str(agent_env.usage_path),
                "agent": "mock",
                "model": None,
                "role": None,
                "started": 0.0,
                "written": False,
            }
        )
        agent_wrapper._write_usage(None)
        agent_wrapper._write_usage(
            ExecutorResult(True, 0, usage={"cost_source": "cli-reported"})
        )
        assert agent_env.manifest["cost_source"] == "unknown"


# --------------------------------------------------------------------------
# commit / push
# --------------------------------------------------------------------------

def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_workdir(tmp_path):
    # NOT "repo": the agent_env fixture already owns tmp_path/"repo".
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "init"], repo)
    return repo


class TestCommitBehavior:
    def test_commit_disabled_performs_no_git_operations(
        self, agent_env, monkeypatch
    ):
        """The dogfood mock-agent step runs with commit disabled: it must
        never be able to push to the repo it is testing."""

        def forbidden(*args, **kwargs):
            raise AssertionError("no git operation may run when commit is off")

        monkeypatch.setattr(agent_wrapper, "_git", forbidden)
        monkeypatch.setattr(
            agent_wrapper.subprocess, "run", forbidden, raising=True
        )

        assert agent_env.run(commit={"enabled": False}) == 0

    def test_commit_enabled_commits_the_agents_changes(
        self, agent_env, git_workdir, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(git_workdir.parent))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_workdir.parent / ".gitconfig"))

        exit_code = agent_env.run(
            repo={"repo_id": "r9", "workdir": str(git_workdir), "branch": "lazyaf/abc"},
            commit={
                "enabled": True,
                "push": False,
                "message": "feat: add rate limiting",
                "author_name": "LazyAF Agent",
                "author_email": "agent@lazyaf.local",
            },
            mock_config={
                "delay_ms": 0,
                "file_operations": [
                    {"action": "create", "path": "src/new.py", "content": "x = 1\n"}
                ],
                "output_events": [],
                "exit_code": 0,
            },
        )

        assert exit_code == 0
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(git_workdir),
            capture_output=True,
            text=True,
        ).stdout
        assert "add rate limiting" in log
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(git_workdir),
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch == "lazyaf/abc"

    def test_push_failure_fails_the_step_but_not_the_usage_row(
        self, agent_env, git_workdir, monkeypatch
    ):
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_workdir.parent / ".gitconfig"))

        exit_code = agent_env.run(
            repo={"repo_id": "r9", "workdir": str(git_workdir), "branch": "lazyaf/abc"},
            commit={"enabled": True, "push": True, "message": "feat: x"},
            mock_config={
                "delay_ms": 0,
                "file_operations": [
                    {"action": "create", "path": "a.txt", "content": "a\n"}
                ],
                "output_events": [],
                "exit_code": 0,
            },
        )

        # No 'origin' remote exists in the fixture, so the push fails.
        assert exit_code == 1
        assert agent_env.manifest is not None
        assert agent_env.manifest["model"] == "mock"

    def test_agent_failure_skips_commit_entirely(
        self, agent_env, monkeypatch
    ):
        def forbidden(*args, **kwargs):
            raise AssertionError("a failed agent must not commit")

        monkeypatch.setattr(agent_wrapper, "_git", forbidden)
        assert agent_env.run(
            commit={"enabled": True, "push": True},
            mock_config={"exit_code": 2, "output_events": [], "delay_ms": 0},
        ) == 2


# --------------------------------------------------------------------------
# log rendering
# --------------------------------------------------------------------------

class TestEventRendering:
    def test_non_json_passes_through_verbatim(self, capsys):
        agent_wrapper._emit("  plain CLI chatter")
        assert capsys.readouterr().out.strip() == "plain CLI chatter"

    def test_result_event_renders_one_readable_line(self, capsys):
        agent_wrapper._emit(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "total_cost_usd": 0.18,
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                }
            )
        )
        out = capsys.readouterr().out.strip()
        assert out.count("\n") == 0
        assert "in=12" in out and "out=3" in out and "cost_usd=0.18" in out

    def test_assistant_text_becomes_one_line(self, capsys):
        agent_wrapper._emit(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Reading\nmain.py"},
                            {"type": "tool_use", "name": "Edit"},
                        ]
                    },
                }
            )
        )
        out = capsys.readouterr().out.strip()
        assert out == "[agent] Reading main.py <tool Edit>"

    def test_long_events_are_truncated(self, capsys):
        agent_wrapper._emit(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "z" * 9000}]},
                }
            )
        )
        assert len(capsys.readouterr().out) < agent_wrapper.MAX_EVENT_LINE + 100

    def test_malformed_json_line_is_not_swallowed(self, capsys):
        agent_wrapper._emit('{"type": "result"')
        assert '{"type": "result"' in capsys.readouterr().out

    def test_empty_assistant_event_prints_nothing(self, capsys):
        agent_wrapper._emit(json.dumps({"type": "assistant", "message": {"content": []}}))
        assert capsys.readouterr().out == ""
