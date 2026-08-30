"""
THE DELETION COMMIT'S OWN TOMBSTONE (Phase 12.6, R2) - designed against
fake-green.

The attempt this phase replaces shipped `test_polling_removal.py`, which
IMPORTED `runner_pool` in order to assert things about it. Three and a half
hours later `runner_pool` was deleted, the module self-skipped, and the test
stayed "green" over a system that could no longer execute agent steps at
all. A removal test that can skip is worse than no removal test: it converts
a loud failure into a silent pass and puts a checkmark next to it.

So this module uses two mechanisms and NEITHER OF THEM CAN SKIP:

  1. `pytest.raises(ModuleNotFoundError)` per deleted module. Re-adding any
     of them is a test FAILURE. There is no import at module scope that
     could vanish, so there is nothing for a deletion to disarm.
  2. A forbidden-token grep over the real source tree. A dangling reference
     the import test cannot possibly see - a compose service, a frontend
     fetch, a yaml key, a docstring that still tells an operator to use a
     removed endpoint - fails here with file:line.

The two are orthogonal on purpose: mechanism 1 catches a module coming back,
mechanism 2 catches a REFERENCE that was never cleaned up. Neither subsumes
the other.

NO importorskip. NO try/except ImportError. NO pytest.mark.skipif. Ever.
If this module ever grows one, it has stopped being a gate.
"""
import importlib
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# `runner-common` is a separate distribution: it is pip-installed into the
# agent images, and scripts/run_tier.py puts it on PYTHONPATH for the tiers.
# Put it there here too, so the surviving-surface assertions below hold under
# a bare `pytest ../tdd` as well - a gate that only works under one invocation
# is a gate that quietly stops running.
_RUNNER_COMMON = REPO_ROOT / "runner-common"
if _RUNNER_COMMON.is_dir() and str(_RUNNER_COMMON) not in sys.path:
    sys.path.insert(0, str(_RUNNER_COMMON))


# -----------------------------------------------------------------------------
# 1. The modules that must stay gone
# -----------------------------------------------------------------------------

GONE = [
    # The in-memory polling pool. Replaced by
    # app.services.execution.runner_registry, which is DB-backed and drives
    # the real RunnerStateMachine.
    "app.services.runner_pool",
    # The job queue and its QueuedJob wire type. Its last live enqueue call
    # site was the agent-only `executor: legacy` hatch, deleted with it.
    "app.services.job_queue",
    # The polling entrypoint monolith and its helpers. The surviving
    # runner-common surface is agent_wrapper / agent_config / executors /
    # usage / git_helpers / pytest_lazyaf - the WRAPPER, not the loop.
    "runner_common.entrypoint",
    "runner_common.job_helpers",
    # build_prompt and friends, superseded by app.services.agent_prompt.
    "runner_common.context_helpers",
]


@pytest.mark.parametrize("module", GONE)
def test_legacy_module_is_gone(module):
    """Re-adding any of these is a test FAILURE, never a silent skip.

    This is the assertion the salvaged attempt's version could not make:
    it asserted things ABOUT the module it wanted deleted, so deleting the
    module disarmed the test.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


# -----------------------------------------------------------------------------
# 2. The references that must stay gone
# -----------------------------------------------------------------------------

#: Tokens that may not appear in live source, config or pipeline definitions.
#: Each one names a thing 12.6 removed; a surviving mention is either dead
#: code or - worse - a live caller of something that no longer exists.
FORBIDDEN = [
    "runner_pool",
    "job_queue",
    "QueuedJob",
    "ExecutorMode.LEGACY",
    "runner-claude",
    "runner-gemini",
    "runner-mock",
    "LAZYAF_USE_LOCAL_EXECUTOR",
    "/api/runners/register",
    "is_playground",
]

#: Where the grep looks. Deliberately the SHIPPING surface: application code,
#: the frontend, the runner-common package, the operational scripts, the
#: dogfood pipeline definitions and compose. Tests are excluded because this
#: module itself has to name every token.
SEARCH_ROOTS = [
    "backend/app",
    "frontend/src",
    "runner-common/runner_common",
    "scripts",
    ".lazyaf",
    "docker-compose.yml",
]

#: PROSE is allowed to remember. PLAN.md records the decision, `upcoming/`
#: holds the design that ordered the deletion, and the historical documents
#: are the post-mortems that explain why. A history that cannot name what it
#: removed is not a history.
ALLOWLIST = [
    "PLAN.md",
    "upcoming/",
    "historical-documents/",
    "docs/",
    "CHANGELOG.md",
    "README.md",
]

#: Extensions worth reading. Binary and lock files are noise.
SEARCHABLE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".svelte", ".yaml", ".yml",
    ".json", ".toml", ".sh", ".ps1", ".cfg", ".ini", ".env",
}

#: Directories never worth reading, at any depth.
SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    ".pytest_cache", "dist", "build", ".svelte-kit", ".mypy_cache",
}


def _is_allowlisted(relative: str) -> bool:
    return any(entry in relative for entry in ALLOWLIST)


def _searchable_files() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        path = REPO_ROOT / root
        if path.is_file():
            files.append(path)
            continue
        if not path.is_dir():
            continue
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            if any(part in SKIP_DIRS for part in candidate.parts):
                continue
            if candidate.suffix and candidate.suffix not in SEARCHABLE_SUFFIXES:
                continue
            files.append(candidate)
    return files


def test_search_roots_all_exist():
    """The grep is only a gate while it is actually looking somewhere.

    A renamed directory would silently reduce this to zero files scanned and
    an unconditional pass - the same fake-green shape in a different costume.
    """
    missing = [r for r in SEARCH_ROOTS if not (REPO_ROOT / r).exists()]
    assert not missing, (
        f"SEARCH_ROOTS point at paths that no longer exist: {missing}. "
        "Fix the list - a grep over nothing passes over everything."
    )


def test_the_grep_actually_reads_files():
    """Companion guard: the file walk must find a substantial corpus."""
    files = _searchable_files()
    assert len(files) > 100, (
        f"only {len(files)} files matched the forbidden-token walk; the "
        "extension or skip-dir filters have gone wrong and this gate is "
        "scanning almost nothing"
    )


@pytest.mark.parametrize("token", FORBIDDEN)
def test_no_forbidden_references(token):
    """A dangling reference the import test cannot see fails here, with
    file:line so it is fixable without a bisect."""
    pattern = re.compile(re.escape(token))
    hits: list[str] = []
    for path in _searchable_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if _is_allowlisted(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{relative}:{number}: {line.strip()[:160]}")

    assert not hits, (
        f"{len(hits)} live reference(s) to the removed {token!r} remain. "
        "Deleting a module while leaving its callers is how a deletion "
        "commit ships a broken system that still passes its unit tests:\n  "
        + "\n  ".join(hits[:40])
    )


# -----------------------------------------------------------------------------
# 3. What SURVIVED - the other half of a deletion
# -----------------------------------------------------------------------------

SURVIVING_RUNNER_COMMON = [
    "runner_common.agent_wrapper",
    "runner_common.agent_config",
    "runner_common.executors",
    "runner_common.usage",
    "runner_common.git_helpers",
    "runner_common.pytest_lazyaf",
]


@pytest.mark.parametrize("module", SURVIVING_RUNNER_COMMON)
def test_runner_common_still_installs_its_surviving_surface(module):
    """`runner-common` still installs into the agent images after the
    monolith around it is gone.

    Deleting entrypoint.py / job_helpers.py / context_helpers.py must not
    take the package's __init__ or its console-script metadata with them -
    a broken import here means every agent step image fails to build, which
    a forbidden-token grep would never notice.
    """
    importlib.import_module(module)


SURVIVING_EXECUTION = [
    "app.services.execution.runner_protocol",
    "app.services.execution.runner_registry",
    "app.services.execution.runner_dispatcher",
    "app.services.execution.remote_executor",
    "app.services.execution.job_recovery",
    "app.services.execution.step_logs",
    "app.services.execution.local_executor",
]


@pytest.mark.parametrize("module", SURVIVING_EXECUTION)
def test_the_replacement_stack_imports(module):
    """The positive half: what replaced the deleted stack must be there.

    Without this, deleting everything in GONE and replacing it with nothing
    would pass every assertion above.
    """
    importlib.import_module(module)
