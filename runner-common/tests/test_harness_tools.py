"""
The six tools and their SANDBOX (Milestone 14.2, design section 3.1).

Two rules are load-bearing here and both are asserted rather than assumed:

1. NOTHING RAISES. Every refusal is a ``ToolResult(is_error=True)`` carrying a
   sentence the model can act on — "path escapes the workspace", "`find`
   matched 0 occurrences ... nearest line 88: ..." — because a traceback ends
   the step while a tool error teaches the model something.
2. A NON-ZERO EXIT IS A RESULT. "the tests failed" is the single most useful
   observation the loop can make; marking it an error would trip
   MAX_CONSECUTIVE_TOOL_ERRORS on a model doing its job correctly.
"""
import json
import os

import pytest

from runner_common.harness.constants import TOOL_OUTPUT_MAX_BYTES
from runner_common.harness.tools import (
    SHELL_DENY_MESSAGE,
    SHELL_ENV_LAZYAF_ALLOWLIST,
    TOOL_ORDER,
    TOOLS,
    ArgSpec,
    Sandbox,
    cap_output,
    changed_path_count,
    run_tool,
    shell_denial_reason,
    tool_schemas,
    validate_args,
    working_tree_changed,
)
from tests.fixtures.openai import make_repo


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


@pytest.fixture
def sandbox(repo):
    return Sandbox(workdir=repo)


# --------------------------------------------------------------------------
# the table itself
# --------------------------------------------------------------------------

def test_there_are_exactly_six_tools():
    """Every additional tool costs schema tokens in EVERY request, which on an
    8k model is a real budget line. Six is a decision, not an accident."""
    assert len(TOOLS) == 6
    assert TOOL_ORDER == (
        "list_files",
        "read_file",
        "write_file",
        "apply_patch",
        "run_shell",
        "finish",
    )
    assert set(TOOLS) == set(TOOL_ORDER)


def test_the_schemas_are_valid_openai_function_tools():
    schemas = tool_schemas()
    assert [s["function"]["name"] for s in schemas] == list(TOOL_ORDER)
    for schema in schemas:
        assert schema["type"] == "function"
        parameters = schema["function"]["parameters"]
        assert parameters["type"] == "object"
        for name in parameters["required"]:
            assert name in parameters["properties"]


def test_deliberately_absent_tools_stay_absent():
    """`search`, `delete_file`, `git_commit` and `git_push` are named as
    absent in the design, each with a reason. Their absence is the contract."""
    for name in ("search", "grep", "delete_file", "git_commit", "git_push"):
        assert name not in TOOLS


# --------------------------------------------------------------------------
# argument validation
# --------------------------------------------------------------------------

class TestArgumentValidation:
    def test_a_missing_required_argument_names_it(self):
        clean, reason = validate_args(TOOLS["read_file"], {})
        assert clean is None
        assert reason == "missing_arg: path"

    def test_a_wrong_type_names_the_expected_json_type(self):
        clean, reason = validate_args(
            TOOLS["read_file"], {"path": "a.py", "max_lines": "200"}
        )
        assert clean is None
        assert reason == "bad_arg_type: max_lines expected integer"

    def test_booleans_are_not_integers_on_this_wire(self):
        _, reason = validate_args(
            TOOLS["read_file"], {"path": "a.py", "max_lines": True}
        )
        assert reason == "bad_arg_type: max_lines expected integer"

    def test_optional_arguments_are_defaulted_not_dropped(self):
        clean, reason = validate_args(TOOLS["read_file"], {"path": "a.py"})
        assert reason is None
        assert clean == {"path": "a.py", "start_line": 1, "max_lines": 400}

    def test_a_non_object_args_payload_is_refused(self):
        _, reason = validate_args(TOOLS["read_file"], ["a.py"])
        assert reason.startswith("bad_args:")

    def test_an_unknown_tool_is_a_tool_error_not_a_crash(self, sandbox):
        result = run_tool(sandbox, "delete_everything", {})
        assert result.is_error
        assert "unknown_tool: delete_everything" in result.text


# --------------------------------------------------------------------------
# the sandbox
# --------------------------------------------------------------------------

class TestSandbox:
    def test_a_parent_traversal_is_a_tool_error(self, sandbox, tmp_path):
        (tmp_path / "secret.txt").write_text("shh", encoding="utf-8")
        result = run_tool(sandbox, "read_file", {"path": "../secret.txt"})
        assert result.is_error is True
        assert "path escapes the workspace" in result.text

    def test_a_deep_traversal_is_a_tool_error(self, sandbox):
        result = run_tool(sandbox, "read_file", {"path": "../../etc/passwd"})
        assert result.is_error is True
        assert "path escapes the workspace" in result.text

    def test_an_absolute_path_outside_the_workspace_is_a_tool_error(
        self, sandbox, tmp_path
    ):
        outside = tmp_path / "outside.txt"
        outside.write_text("shh", encoding="utf-8")
        result = run_tool(sandbox, "read_file", {"path": str(outside)})
        assert result.is_error is True
        assert "path escapes the workspace" in result.text

    def test_a_symlink_out_of_the_workspace_is_a_tool_error(self, sandbox, tmp_path):
        """``os.path.realpath`` FIRST, so a symlink is caught by the same
        check as ``../..``.

        Symlink creation needs privileges on Windows. Where it is unavailable
        the equivalent unresolved traversal is asserted instead, so the rule
        is covered on every platform without a conditional skip.
        """
        outside = tmp_path / "vault"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("shh", encoding="utf-8")
        link = sandbox.workdir / "escape"
        try:
            os.symlink(str(outside), str(link), target_is_directory=True)
            supported = True
        except (OSError, NotImplementedError, AttributeError):
            supported = False
        if supported:
            result = run_tool(sandbox, "read_file", {"path": "escape/secret.txt"})
            assert result.is_error is True
            assert "path escapes the workspace" in result.text
        result = run_tool(sandbox, "read_file", {"path": "../vault/secret.txt"})
        assert result.is_error is True

    def test_the_control_directory_is_denied_belt_and_braces(self, sandbox):
        """It holds a sibling step's config and the usage manifest."""
        control = sandbox.workdir / ".control"
        control.mkdir()
        (control / "agent.json").write_text("{}", encoding="utf-8")
        result = run_tool(sandbox, "read_file", {"path": ".control/agent.json"})
        assert result.is_error is True
        assert "control directory" in result.text

    def test_an_empty_path_is_a_tool_error(self, sandbox):
        result = run_tool(sandbox, "read_file", {"path": "   "})
        assert result.is_error is True


# --------------------------------------------------------------------------
# list_files / read_file / write_file
# --------------------------------------------------------------------------

class TestReadingAndWriting:
    def test_list_files_elides_beyond_max_entries(self, sandbox):
        for index in range(30):
            (sandbox.workdir / f"f{index:02d}.txt").write_text("x", encoding="utf-8")
        result = run_tool(sandbox, "list_files", {"path": ".", "max_entries": 5})
        assert result.is_error is False
        assert "[28 more elided]" in result.text
        assert len(result.text.splitlines()) == 6

    def test_list_files_skips_the_git_directory(self, sandbox):
        result = run_tool(sandbox, "list_files", {"path": ".", "depth": 3})
        assert ".git/" not in result.text

    def test_read_file_is_ranged_and_reports_the_total(self, sandbox):
        big = "\n".join(f"line {index}" for index in range(1, 1001))
        (sandbox.workdir / "big.py").write_text(big, encoding="utf-8")
        result = run_tool(
            sandbox, "read_file", {"path": "big.py", "start_line": 500, "max_lines": 3}
        )
        assert "total_lines=1000" in result.text
        assert " 500 | line 500" in result.text
        assert "line 503" not in result.text
        assert result.summary == "lines 500-502 of 1000"

    def test_read_file_output_is_capped_head_and_tail(self, sandbox):
        (sandbox.workdir / "huge.txt").write_text("A" * 40_000, encoding="utf-8")
        result = run_tool(sandbox, "read_file", {"path": "huge.txt"})
        assert "bytes elided" in result.text
        assert len(result.text.encode("utf-8")) < TOOL_OUTPUT_MAX_BYTES + 200

    def test_cap_output_keeps_the_head_and_the_tail(self):
        text = "HEAD" + ("x" * 5000) + "TAIL"
        capped = cap_output(text, 200)
        assert capped.startswith("HEAD")
        assert capped.endswith("TAIL")
        assert "bytes elided" in capped

    def test_write_file_reports_bytes_and_created(self, sandbox):
        first = run_tool(
            sandbox, "write_file", {"path": "pkg/new.py", "content": "print(1)\n"}
        )
        assert json.loads(first.text) == {"bytes": 9, "created": True}
        second = run_tool(
            sandbox, "write_file", {"path": "pkg/new.py", "content": "print(2)\n"}
        )
        assert json.loads(second.text)["created"] is False
        assert (sandbox.workdir / "pkg" / "new.py").read_text() == "print(2)\n"


# --------------------------------------------------------------------------
# apply_patch
# --------------------------------------------------------------------------

class TestApplyPatch:
    def test_an_exact_find_replaces_and_reports_counts(self, sandbox):
        result = run_tool(
            sandbox,
            "apply_patch",
            {"path": "src/main.py", "find": "return 1", "replace": "return 2"},
        )
        assert result.is_error is False
        assert json.loads(result.text) == {"occurrences": 1, "applied": 1}
        assert "return 2" in (sandbox.workdir / "src" / "main.py").read_text()

    def test_zero_occurrences_is_an_error_naming_the_nearest_line(self, sandbox):
        result = run_tool(
            sandbox,
            "apply_patch",
            {"path": "src/main.py", "find": "    return 42", "replace": "x"},
        )
        assert result.is_error is True
        assert "matched 0 occurrences in src/main.py" in result.text
        assert "nearest line 2: `return 1`" in result.text
        assert "must match the file EXACTLY" in result.text

    def test_count_zero_replaces_every_occurrence(self, sandbox):
        (sandbox.workdir / "many.txt").write_text("a\na\na\n", encoding="utf-8")
        result = run_tool(
            sandbox,
            "apply_patch",
            {"path": "many.txt", "find": "a", "replace": "b", "count": 0},
        )
        assert json.loads(result.text) == {"occurrences": 3, "applied": 3}
        assert (sandbox.workdir / "many.txt").read_text() == "b\nb\nb\n"

    def test_patching_a_missing_file_is_a_tool_error(self, sandbox):
        result = run_tool(
            sandbox, "apply_patch", {"path": "nope.py", "find": "a", "replace": "b"}
        )
        assert result.is_error is True
        assert "no such file" in result.text


# --------------------------------------------------------------------------
# run_shell
# --------------------------------------------------------------------------

class TestRunShell:
    def test_a_non_zero_exit_is_a_result_not_an_error(self, sandbox):
        result = run_tool(sandbox, "run_shell", {"command": "exit 7"})
        assert result.is_error is False, "a failing test suite is an OBSERVATION"
        assert json.loads(result.text)["exit_code"] == 7

    def test_stdout_reaches_the_model(self, sandbox):
        result = run_tool(sandbox, "run_shell", {"command": "echo hello-harness"})
        payload = json.loads(result.text)
        assert payload["exit_code"] == 0
        assert "hello-harness" in payload["stdout"]

    def test_git_push_is_denied_and_the_message_names_the_platform(self, sandbox):
        result = run_tool(
            sandbox, "run_shell", {"command": "git push origin HEAD"}
        )
        assert result.is_error is True
        assert SHELL_DENY_MESSAGE in result.text

    @pytest.mark.parametrize(
        "command",
        [
            "git push",
            "cd . && git push origin main",
            "echo hi; git remote add evil http://x/y",
            "git config --global user.email x@y.z",
            "git -C . push",
        ],
    )
    def test_the_denylist_is_small_explicit_and_effective(self, command):
        assert shell_denial_reason(command) is not None

    @pytest.mark.parametrize(
        "command",
        ["git status --porcelain", "git add -A", "git commit -m x", "git diff", "pytest -q"],
    )
    def test_harmless_git_stays_allowed(self, command):
        assert shell_denial_reason(command) is None

    def test_a_credentialed_url_is_denied(self):
        remote = "http://lazyaf:tok3n@backend:8000/git/r9.git"
        assert shell_denial_reason(f"git fetch {remote}", remote) is not None
        assert shell_denial_reason(f"curl {remote}") is not None

    def test_the_shell_child_never_sees_the_endpoint_key(self, repo):
        sandbox = Sandbox(
            workdir=repo,
            api_key_env="LAZYAF_ENDPOINT_API_KEY",
            api_key_value="sk-sentinel-value-0123",
            base_env={
                "PATH": os.environ.get("PATH", ""),
                "LAZYAF_ENDPOINT_API_KEY": "sk-sentinel-value-0123",
                "LAZYAF_STEP_AUTH_SECRET": "jwt-goes-here",
                "LAZYAF_PIPELINE_RUN_ID": "p1",
                "LAZYAF_STEP_RUN_ID": "s1",
                "SNEAKY_COPY": "sk-sentinel-value-0123",
                "HOME": os.environ.get("HOME", ""),
            },
        )
        env = sandbox.shell_env()
        assert "LAZYAF_ENDPOINT_API_KEY" not in env
        assert "LAZYAF_STEP_AUTH_SECRET" not in env
        assert "SNEAKY_COPY" not in env, "the VALUE is stripped under any name"
        assert set(SHELL_ENV_LAZYAF_ALLOWLIST) <= set(env)
        assert "sk-sentinel-value-0123" not in json.dumps(env)

    def test_output_is_capped(self, sandbox):
        result = run_tool(
            sandbox,
            "run_shell",
            {"command": "python -c \"print('z'*40000)\""},
        )
        payload = json.loads(result.text)
        assert len(payload["stdout"].encode("utf-8")) <= TOOL_OUTPUT_MAX_BYTES

    def test_a_missing_shell_is_a_tool_error_not_a_crash(self, repo):
        sandbox = Sandbox(workdir=repo, shell=("definitely-not-a-shell", "-c"))
        result = run_tool(sandbox, "run_shell", {"command": "echo hi"})
        assert result.is_error is True
        assert "could not start a shell" in result.text


# --------------------------------------------------------------------------
# the ONE read-only git observation
# --------------------------------------------------------------------------

class TestWorkingTreeObservation:
    def test_a_clean_tree_reports_no_change(self, repo):
        assert working_tree_changed(repo) is False
        assert changed_path_count(repo) == 0

    def test_a_new_file_is_a_change(self, repo):
        (repo / "new.txt").write_text("x", encoding="utf-8")
        assert working_tree_changed(repo) is True
        assert changed_path_count(repo) == 1

    def test_a_non_repository_answers_false_rather_than_raising(self, tmp_path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert working_tree_changed(plain) is False
