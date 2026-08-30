"""CLI wiring and the packaging metadata failure_01 shipped broken.

Two small things with disproportionate blast radius:

* ``pyproject.toml`` declaring a README that does not exist makes
  ``pip install .`` fail at METADATA GENERATION - before a single line of the
  agent runs, and with an error that says nothing about the agent. failure_01
  shipped exactly that, so the agent could not be installed at all.
* An orchestrator whose ``preflight()`` fails must stop the process, not
  register anyway. A runner that appears in the list and then fails every
  assignment is harder to diagnose than one that never appears and says why.

``pyproject.toml`` is read with a small regex rather than ``tomllib``: this
package supports Python 3.10, where ``tomllib`` does not exist, and adding a
``tomli`` dependency to a runner host for one assertion is the wrong trade.
"""
from __future__ import annotations

import re

from lazyaf_runner import __version__
from lazyaf_runner.cli import build_parser, config_from_args, main, run_agent
from lazyaf_runner.client import EXIT_FATAL, EXIT_OK
from lazyaf_runner.orchestrator.base import OrchestratorUnavailable
from lazyaf_runner.orchestrator.registry import ORCHESTRATORS

from conftest import RUNNER_AGENT_DIR, StubOrchestrator, make_config

PYPROJECT = RUNNER_AGENT_DIR / "pyproject.toml"
PYPROJECT_TEXT = PYPROJECT.read_text(encoding="utf-8")


def _scalar(key: str) -> str | None:
    match = re.search(rf'^{key}\s*=\s*"([^"]+)"', PYPROJECT_TEXT, re.MULTILINE)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def test_declared_readme_actually_exists() -> None:
    readme = _scalar("readme")
    assert readme, "the package should describe itself"
    assert (RUNNER_AGENT_DIR / readme).exists(), (
        f"pyproject declares readme={readme!r} but the file is missing - "
        "`pip install .` then fails at metadata generation, before any agent "
        "code runs, with an error that says nothing about the agent"
    )


def test_console_script_resolves() -> None:
    assert "lazyaf-runner = \"lazyaf_runner.cli:main\"" in PYPROJECT_TEXT
    assert callable(main)


def test_declared_version_matches_the_package() -> None:
    """The version rides on `register.agent_version`; a stale one makes a
    fleet's forensics lie."""
    assert _scalar("version") == __version__


def test_runtime_dependencies_are_declared() -> None:
    """A runner host installs this package and nothing else."""
    assert "websockets" in PYPROJECT_TEXT
    assert "docker" in PYPROJECT_TEXT


def test_the_package_never_imports_the_backend() -> None:
    """The file-ownership rule, checked rather than remembered: a runner host
    does not have `backend/app` and never will."""
    offenders = []
    for path in (RUNNER_AGENT_DIR / "lazyaf_runner").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import app", "from app.", "from app ")):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders, f"runner package imports the backend: {offenders}"


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------

def test_orchestrator_choices_come_from_the_registry() -> None:
    """Adding an orchestrator must not require editing the parser."""
    action = next(
        a for a in build_parser()._actions if a.option_strings == ["--orchestrator"]
    )
    assert set(action.choices) == set(ORCHESTRATORS)


def test_config_from_args_with_no_arguments_is_usable() -> None:
    config = config_from_args([], env={})
    assert config.runner_id
    assert config.name == config.runner_id
    assert config.ws_url.endswith("/ws/runner")


# ---------------------------------------------------------------------------
# run_agent
# ---------------------------------------------------------------------------

async def test_preflight_failure_stops_the_process() -> None:
    unavailable = OrchestratorUnavailable("no docker socket on this host")
    orch = StubOrchestrator(preflight_error=unavailable)
    ORCHESTRATORS["stub-preflight"] = lambda config, **kwargs: orch  # type: ignore[assignment]
    try:
        code = await run_agent(make_config(orchestrator="stub-preflight"))
    finally:
        ORCHESTRATORS.pop("stub-preflight", None)

    assert code == EXIT_FATAL
    assert not orch.preflighted


async def test_unknown_orchestrator_stops_the_process() -> None:
    assert await run_agent(make_config(orchestrator="does-not-exist")) == EXIT_FATAL


async def test_shutdown_runs_even_when_the_client_returns() -> None:
    """Workspaces this process created must be reaped when it exits, or a
    long-lived runner host slowly fills up with dead volumes."""
    orch = StubOrchestrator()

    class NoOpClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def stop(self) -> None:
            pass

        async def run(self) -> int:
            return EXIT_OK

    import lazyaf_runner.cli as cli_module

    ORCHESTRATORS["stub-shutdown"] = lambda config, **kwargs: orch  # type: ignore[assignment]
    original_client = cli_module.RunnerClient
    cli_module.RunnerClient = NoOpClient  # type: ignore[assignment]
    try:
        code = await run_agent(make_config(orchestrator="stub-shutdown"))
    finally:
        cli_module.RunnerClient = original_client  # type: ignore[assignment]
        ORCHESTRATORS.pop("stub-shutdown", None)

    assert code == EXIT_OK
    assert orch.preflighted
    assert orch.shutdowns == 1


def test_main_rejects_an_unusable_config(capsys) -> None:
    code = main(["--backend-url", "ftp://nope"])
    assert code == EXIT_FATAL
    assert "lazyaf-runner:" in capsys.readouterr().err
