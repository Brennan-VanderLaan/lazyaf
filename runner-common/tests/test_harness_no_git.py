"""
Commit ownership stayed with the 12.5 wrapper (design section 3.6).

``HarnessExecutor.execute()`` returns an ``ExecutorResult`` and lands nothing.
``agent_wrapper._finish(cfg, result)`` then does exactly what it does for
claude and gemini today: ``configure_git``, ``checkout -B``, ``add -A``,
``commit``, ``push``. There is no second implementation, which is also why
``run_shell`` denies ``git push``: letting the model push would create a
second, unpoliced path to the remote and re-fire the push trigger that started
the run.

STATED DEVIATION FROM THE WAVE-8 TEST CONTRACT (agent B, item 7). That contract
says the harness "neither imports ``git_helpers`` nor invokes ``git``", while
design section 3.5 says in the same document that "the tree changed" is
``git status --porcelain`` "computed by the harness before it returns". Those
two sentences cannot both be satisfied literally. This file honours section
3.5's mechanism — it is the specific one, and the success rule depends on it —
and narrows the assertion to what the contract's own stated purpose is:

  * the harness imports ``git_helpers`` NOWHERE, and
  * across a whole run the ONLY git invocation is the read-only
    ``git status --porcelain``: no add, commit, checkout, push, remote or
    config.

That is "commit ownership stayed with the wrapper", asserted.
"""
import ast
import subprocess
from pathlib import Path

import pytest

from runner_common.harness import executor as executor_module
from tests.fixtures.openai import (
    DEFAULT_USAGE_SERIES,
    chat_response,
    make_repo,
    run_harness,
    tool_call,
)

HARNESS_DIR = Path(executor_module.__file__).parent

#: The subcommands that LAND work. None of them may appear in the harness.
MUTATING_GIT_SUBCOMMANDS = (
    "commit",
    "push",
    "checkout",
    "add",
    "remote",
    "reset",
    "merge",
    "rebase",
    "cherry-pick",
    "tag",
)


def harness_modules():
    return sorted(path for path in HARNESS_DIR.glob("*.py"))


def test_the_harness_package_never_imports_git_helpers():
    for path in harness_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "git_helpers" not in alias.name, path.name
            elif isinstance(node, ast.ImportFrom):
                assert "git_helpers" not in (node.module or ""), path.name
                for alias in node.names:
                    assert alias.name != "git_helpers", path.name


def test_the_executor_module_names_no_mutating_git_subcommand():
    source = (HARNESS_DIR / "executor.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="executor.py")
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    for literal in literals:
        assert literal != "git", "executor.py must not build a git argv"


@pytest.mark.parametrize("subcommand", MUTATING_GIT_SUBCOMMANDS)
def test_no_harness_module_builds_a_mutating_git_argv(subcommand):
    """A string ``"git"`` next to a mutating subcommand in the same argv is
    the shape being forbidden; ``tools.py`` may name them only inside the
    DENYLIST, which is a refusal, not an invocation."""
    for path in harness_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            values = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if "git" in values:
                assert subcommand not in values, f"{path.name} builds `git {subcommand}`"


def test_a_whole_run_invokes_git_exactly_once_and_read_only(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    seen = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        if argv and str(argv[0]).endswith("git"):
            seen.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    result, _, _, _ = run_harness(
        repo,
        [
            chat_response(
                tool_calls=[tool_call("write_file", {"path": "d.txt", "content": "x"})],
                usage=dict(DEFAULT_USAGE_SERIES[0]),
            ),
            chat_response(
                tool_calls=[tool_call("finish", {"status": "success", "summary": "ok"})],
                usage=dict(DEFAULT_USAGE_SERIES[1]),
            ),
        ],
    )
    assert result.success is True
    assert seen == [["git", "status", "--porcelain"]]


def test_the_model_cannot_push_even_when_it_tries(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    seen = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        seen.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    result, logs, _, _ = run_harness(
        repo,
        [
            chat_response(
                tool_calls=[tool_call("run_shell", {"command": "git push -u origin HEAD"})],
                usage=dict(DEFAULT_USAGE_SERIES[0]),
            ),
            chat_response(
                tool_calls=[tool_call("finish", {"status": "blocked", "summary": "denied"})],
                usage=dict(DEFAULT_USAGE_SERIES[1]),
            ),
        ],
    )
    assert result.success is False
    shell_calls = [argv for argv in seen if argv and argv[0] == "bash"]
    assert shell_calls == [], "the denylist refused BEFORE a shell was spawned"
    assert any("do not push" in line for line in logs)
