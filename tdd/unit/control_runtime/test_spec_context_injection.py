"""
Wrapper-side behaviour of the CURATED SPEC CONTEXT (12.6.6).

`runner_common.agent_wrapper` is the CONSUMER end of the contract in
`spec_context_contract.py`. What it owes the rest of the system:

1. MATERIALISE the bundle at `<control dir>/spec_context.md`, so an agent 40
   turns into a session can `cat` its brief instead of trusting its own
   context window. The path is DERIVED from the config path the backend
   announced - never taken from the payload.
2. SAY WHAT IT GOT, always. A bundle logs its size and truncation facts; no
   bundle logs one line saying so. A silent absence is indistinguishable from
   a bug that dropped the brief (R1).
3. DELETE it on the way out. The workspace volume outlives the step, and step
   N+1's agent must never read step N's brief.
4. NEVER RE-RENDER. The backend owns the prompt (R3); the wrapper does not
   prepend, append, or touch it.
5. NEVER FAIL THE STEP over any of the above.

These tests live under `tdd/` rather than in `runner-common/tests/` on
purpose: `scripts/run_tier.py` selects no path under `runner-common/`, so a
test written there would be ungated (R4). They drive the REAL `main()` over
real files, in the style of `runner-common/tests/test_agent_wrapper.py`.

The conftest of this package already puts `runner-common` on sys.path.
"""
import copy
import json
import os
from pathlib import Path

import pytest

from runner_common import agent_wrapper
from runner_common.agent_config import (
    AGENT_CONFIG_PATH_ENV,
    SPEC_CONTEXT_FILENAME,
)
from runner_common.executors import ExecutorResult
from runner_common.usage import USAGE_PATH_ENV

from tdd.unit.control_runtime.spec_context_contract import (
    CANONICAL_BUNDLE,
    TRUNCATED_BUNDLE,
    assert_bundle_conforms,
)


class ObservingExecutor:
    """Records what the wrapper handed it AND what the disk looked like
    WHILE the agent was running - which is the only moment the file has to
    exist."""

    last = None

    def __init__(self, **kwargs):
        self.prompt = None
        self.spec_file_existed = None
        self.spec_file_text = None
        self.spec_path = None
        ObservingExecutor.last = self

    def execute(self, config, log_callback=None, streaming=True):
        self.prompt = config.prompt
        if self.spec_path is not None:
            self.spec_file_existed = self.spec_path.exists()
            if self.spec_file_existed:
                self.spec_file_text = self.spec_path.read_text(encoding="utf-8")
        return ExecutorResult(
            success=True,
            exit_code=0,
            stdout="",
            usage={"provider": "anthropic", "cost_source": "cli-reported"},
        )


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """Write an agent config, point the wrapper at it, run the REAL main().

    Lifted from `runner-common/tests/test_agent_wrapper.py`'s fixture rather
    than imported, because that package is outside every tier and this suite
    must not depend on an ungated file.
    """

    class Harness:
        def __init__(self):
            self.control_dir = tmp_path
            self.config_path = tmp_path / "agent.exec-1.json"
            self.usage_path = tmp_path / "usage.exec-1.json"
            self.spec_path = tmp_path / SPEC_CONTEXT_FILENAME
            self.workdir = tmp_path / "repo"
            self.workdir.mkdir()

        def write(self, **overrides):
            data = {
                "version": 1,
                "agent": "mock",
                "model": "claude-haiku-4-5",
                "stream": False,
                "prompt": "Implement the feature.",
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
                "spec_context": None,
            }
            data.update(overrides)
            self.config_path.write_text(json.dumps(data), encoding="utf-8")
            monkeypatch.setenv(AGENT_CONFIG_PATH_ENV, str(self.config_path))
            monkeypatch.setenv(USAGE_PATH_ENV, str(self.usage_path))
            return self

        def run(self, **overrides):
            self.write(**overrides)
            return agent_wrapper.main()

        def run_observed(self, **overrides):
            """Run with an executor that inspects the disk mid-step."""
            observer_holder = {}

            def build(cfg):
                executor = ObservingExecutor()
                executor.spec_path = self.spec_path
                observer_holder["executor"] = executor
                return executor

            monkeypatch.setitem(agent_wrapper.EXECUTORS, "mock", build)
            exit_code = self.run(**overrides)
            return exit_code, observer_holder["executor"]

    return Harness()


def _bundle(**overrides):
    payload = copy.deepcopy(CANONICAL_BUNDLE)
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# materialisation
# ---------------------------------------------------------------------------

class TestBundleIsWrittenToTheWorkspace:
    def test_bundle_written_to_workspace_while_the_agent_runs(self, agent_env):
        bundle = _bundle()
        assert_bundle_conforms(bundle, "CONSUMER (fixture input)")

        exit_code, executor = agent_env.run_observed(spec_context=bundle)

        assert exit_code == 0
        assert executor.spec_file_existed is True, (
            "the brief must exist WHILE the agent is running - that is the "
            "only moment it is useful"
        )
        assert executor.spec_file_text == bundle["markdown"]

    def test_the_path_is_derived_from_the_config_path_not_the_payload(
        self, agent_env
    ):
        """The wrapper never writes to a path a payload told it to."""
        bundle = _bundle()
        bundle["source"] = dict(bundle["source"])
        _exit_code, executor = agent_env.run_observed(spec_context=bundle)

        assert executor.spec_path == agent_env.config_path.parent / "spec_context.md"

    def test_the_filename_is_the_contracted_one(self, agent_env):
        assert SPEC_CONTEXT_FILENAME == "spec_context.md"
        _exit_code, executor = agent_env.run_observed(spec_context=_bundle())
        assert executor.spec_file_existed is True

    def test_bundle_file_is_deleted_after_the_step(self, agent_env):
        """Consume-once on a shared volume: step N+1's agent must never read
        step N's brief."""
        agent_env.run(spec_context=_bundle())

        assert not agent_env.spec_path.exists()

    def test_bundle_file_is_deleted_even_when_the_agent_fails(self, agent_env):
        exit_code = agent_env.run(
            spec_context=_bundle(),
            mock_config={
                "response_mode": "batch",
                "delay_ms": 0,
                "file_operations": [],
                "output_events": [{"type": "error", "text": "boom"}],
                "exit_code": 3,
            },
        )

        assert exit_code != 0
        assert not agent_env.spec_path.exists()

    def test_bundle_file_mode_is_0600(self, agent_env, monkeypatch):
        """0600, matching the tar's file mode.

        Asserted on EVERY platform by intercepting the syscall - a
        `skipif(os.name != 'posix')` here would be a conditional standing in
        front of the only assertion (R4). The POSIX leg additionally checks
        the mode the filesystem actually recorded.
        """
        modes = []
        real_open = os.open

        def recording_open(path, flags, mode=0o777, *args, **kwargs):
            if str(path).endswith(SPEC_CONTEXT_FILENAME):
                modes.append(mode)
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(agent_wrapper.os, "open", recording_open)

        _exit_code, executor = agent_env.run_observed(spec_context=_bundle())

        assert modes == [0o600]
        if os.name == "posix":
            # The file is gone by now (consume-once), so the on-disk check
            # rides the mid-step observation instead.
            assert executor.spec_file_existed is True


# ---------------------------------------------------------------------------
# the clean no-op
# ---------------------------------------------------------------------------

class TestNoBundle:
    def test_no_bundle_writes_no_file_and_logs_exactly_one_line(
        self, agent_env, capsys
    ):
        exit_code = agent_env.run(spec_context=None)
        out = capsys.readouterr().out

        assert exit_code == 0
        assert not agent_env.spec_path.exists()
        spec_lines = [
            line for line in out.splitlines() if "spec context" in line.lower()
        ]
        assert spec_lines == [
            "[agent] spec context: none (no spec links for this card)"
        ], (
            "a SILENT absence is indistinguishable from a bug that dropped "
            "the brief (R1) - and an empty '## Spec Context' heading would be "
            "the noise this phase exists to avoid"
        )

    def test_a_pre_12_6_6_payload_is_the_same_no_op(self, agent_env, capsys):
        """The key is simply absent: an older backend, a newer wrapper."""
        agent_env.write()
        payload = json.loads(agent_env.config_path.read_text(encoding="utf-8"))
        payload.pop("spec_context")
        agent_env.config_path.write_text(json.dumps(payload), encoding="utf-8")

        exit_code = agent_wrapper.main()

        assert exit_code == 0
        assert not agent_env.spec_path.exists()
        assert "no spec links for this card" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# what the operator sees
# ---------------------------------------------------------------------------

class TestLogging:
    def test_the_log_line_carries_the_counts_and_the_estimate(
        self, agent_env, capsys
    ):
        agent_env.run(spec_context=_bundle())
        out = capsys.readouterr().out

        assert "[agent] spec context: 1 criteria, 1 related tests" in out
        assert f"~{CANONICAL_BUNDLE['estimated_tokens']} tokens" in out
        assert "truncated=False" in out
        assert SPEC_CONTEXT_FILENAME in out

    def test_truncated_bundle_logs_the_dropped_rules(self, agent_env, capsys):
        agent_env.run(spec_context=copy.deepcopy(TRUNCATED_BUNDLE))
        out = capsys.readouterr().out

        assert "truncated=True" in out
        assert (
            "[agent] note: spec context was truncated (dropped: "
            "criterion_notes, feature_description)" in out
        )

    def test_an_untruncated_bundle_does_not_claim_it_was_truncated(
        self, agent_env, capsys
    ):
        agent_env.run(spec_context=_bundle())
        assert "was truncated" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the backend is the single renderer (D2)
# ---------------------------------------------------------------------------

class TestTheWrapperNeverReRenders:
    def test_the_executor_gets_the_backends_prompt_byte_for_byte(
        self, agent_env
    ):
        """R3. The wrapper does NOT prepend the bundle to the prompt and does
        not add a system-prompt flag: the backend already rendered the one
        prompt, and a second producer of the most important string in the
        system is exactly what 12.5 moved rendering backend-side to avoid.
        """
        prompt = "Implement the feature.\n\n" + CANONICAL_BUNDLE["markdown"]
        _exit_code, executor = agent_env.run_observed(
            prompt=prompt, spec_context=_bundle()
        )

        assert executor.prompt == prompt

    def test_a_bundle_does_not_leak_into_the_prompt_by_itself(self, agent_env):
        """The backend decides whether the markdown is in the prompt. When it
        is not, the wrapper must not put it there."""
        _exit_code, executor = agent_env.run_observed(
            prompt="Implement the feature.", spec_context=_bundle()
        )

        assert executor.prompt == "Implement the feature."


# ---------------------------------------------------------------------------
# never fatal
# ---------------------------------------------------------------------------

class TestFailureIsLoudButNotFatal:
    def test_unwritable_control_dir_warns_and_the_step_still_runs(
        self, agent_env, monkeypatch, capsys
    ):
        real_open = os.open

        def refusing_open(path, flags, mode=0o777, *args, **kwargs):
            if str(path).endswith(SPEC_CONTEXT_FILENAME):
                raise OSError(13, "Permission denied")
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(agent_wrapper.os, "open", refusing_open)

        exit_code = agent_env.run(spec_context=_bundle())
        captured = capsys.readouterr()

        assert exit_code == 0, (
            "the same text is already in the prompt; killing a real agent run "
            "over a convenience file would be a worse outcome than its absence"
        )
        assert "WARNING" in captured.err
        assert SPEC_CONTEXT_FILENAME in captured.err
        assert "Permission denied" in captured.err
        assert not agent_env.spec_path.exists()

    def test_a_malformed_bundle_is_a_refusal_not_a_crash(
        self, agent_env, capsys
    ):
        """`spec_context` present but not an object: the loader refuses the
        whole config rather than guessing. A wrapper that half-understands
        its instructions is worse than one that refuses."""
        exit_code = agent_env.run(spec_context="## Spec Context")

        assert exit_code == 1
        assert "spec_context" in capsys.readouterr().err
        assert not agent_env.spec_path.exists()

    def test_a_bundle_with_null_markdown_is_treated_as_no_bundle(
        self, agent_env, capsys
    ):
        bundle = _bundle(markdown=None)
        exit_code = agent_env.run(spec_context=bundle)

        assert exit_code == 0
        assert not agent_env.spec_path.exists()
        assert "no spec links for this card" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the usage manifest keeps working (12.5 contract #2 is not disturbed)
# ---------------------------------------------------------------------------

class TestTelemetryIsUndisturbed:
    def test_the_usage_manifest_is_still_written(self, agent_env):
        agent_env.run(spec_context=_bundle())

        manifest = json.loads(agent_env.usage_path.read_text(encoding="utf-8"))
        assert manifest["version"] == 1
        assert "spec_context" not in manifest

    def test_the_spec_file_is_not_the_agent_config(self, agent_env):
        """Two files, two lifecycles, both consumed."""
        agent_env.run(spec_context=_bundle())

        assert not agent_env.config_path.exists()
        assert not agent_env.spec_path.exists()
        assert Path(agent_env.usage_path).exists()

