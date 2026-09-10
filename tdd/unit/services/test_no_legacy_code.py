"""
THE DELETION COMMIT'S OWN TOMBSTONE (Phase 12.6, R2) - designed against
fake-green.

The attempt this phase replaces shipped `test_polling_removal.py`, which
IMPORTED `runner_pool` in order to assert things about it. Three and a half
hours later `runner_pool` was deleted, the module self-skipped, and the test
stayed "green" over a system that could no longer execute agent steps at
all. A removal test that can skip is worse than no removal test: it converts
a loud failure into a silent pass and puts a checkmark next to it.

So this module uses three mechanisms and NONE OF THEM CAN SKIP:

  1. `pytest.raises(ModuleNotFoundError)` per deleted module. Re-adding any
     of them is a test FAILURE. There is no import at module scope that
     could vanish, so there is nothing for a deletion to disarm.
  2. A forbidden-token grep over the real source tree. A dangling reference
     the import test cannot possibly see - a compose service, a frontend
     fetch, a yaml key, a docstring that still tells an operator to use a
     removed endpoint - fails here with file:line.
  3. ABSENCE OF SYMBOL, and absence of a PARAMETER (section 2b). Added at
     12.8 for the v1 array pipeline format, which had no module of its own:
     it was a fork inside a module that survives. Mechanism 1 cannot see a
     retired FUNCTION and mechanism 2 cannot tell a live caller from a
     docstring, so a two-way branch coming back under a new name needs a
     third shape of assertion.

The three are orthogonal on purpose: 1 catches a module coming back, 2
catches a REFERENCE that was never cleaned up, 3 catches a fork being
re-opened inside surviving code. None subsumes the others.

NO importorskip. NO try/except ImportError. NO pytest.mark.skipif. Ever.
If this module ever grows one, it has stopped being a gate.
"""
import importlib
import inspect
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect

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
    # --- 12.8: the v1 array pipeline format -------------------------------
    # The two-way fork itself. Every executor method that took it is gone and
    # `steps_graph` is the only definition a pipeline has, so a surviving
    # `is_graph` is either dead code or a second format creeping back.
    "is_graph",
    # v1's action dispatcher and its closed vocabulary. `_handle_action` read
    # `on_success`/`on_failure` as an instruction to the RUN; the graph reads
    # flow off edges and effects off `node.actions`.
    "_handle_action",
    "STEP_ACTION_PREFIXES",
    "_trigger_pipeline",
    # The array being WRITTEN. `steps` is not a column any more (0015), so
    # this spelling can only be a writer that will fail at runtime.
    "steps=json.dumps",
]

#: NOT forbidden, and deliberately so - recorded here because each one looks
#: like it belongs above and banning it would break something real.
#:
#: `on_success` / `on_failure`: they survive on `PipelineStepYaml` and
#:     `PipelineStepConfig` at the AUTHORING edge. The decision was that a
#:     human writing five sequential steps should not hand-author nodes and
#:     edges, so the array is still the repo-YAML dialect and the boundary
#:     converts it. A blanket ban would forbid the thing 12.8 deliberately
#:     kept.
#: `steps`: far too common a word - `steps_graph`, `steps_total`,
#:     `steps_completed`, `graph["steps"]` and `PipelineYaml.steps` are all
#:     live. The column's absence is asserted structurally instead, in
#:     `test_pipelines_table_has_no_steps_column`.
#: `parse_steps`: `parse_steps_graph` is live and shares the prefix. Its
#:     absence is asserted by symbol in section 2b.
#: `continue_in_context`: stays on all three schemas, accepted-and-ignored
#:     with a one-time log. Removing it from the schema would make pydantic
#:     silently DROP it from user YAML, which is a fresh R1 violation.
#: `describe_step_action` / `has_graph_definition`: both are genuinely gone,
#:     but the only surviving mentions are PROSE IN LIVE SOURCE explaining
#:     what replaced them (`schemas/pipeline.py`'s "the graph twin of
#:     `describe_step_action`", `models/pipeline.py`'s note on why the
#:     has-graph predicate went with the fork it fed). Mechanism 2 cannot tell
#:     a comment from a call, and deleting those comments to satisfy a grep
#:     would trade real explanation for a green check. They are asserted by
#:     SYMBOL in section 2b instead, where a name collision is impossible.
#: `.lazyaf-context`: NOT RETIRED. Section 5.4 of the retirement plan listed
#:     it, and that was wrong against this tree - `pipeline_executor.py`'s
#:     `_execute_trigger_action` still calls
#:     `git_repo_manager.delete_directory_from_branch(directory=".lazyaf-context")`
#:     to clean the directory off a merged branch (Phase 9.1d), and
#:     `runner_common` still names it. What 12.6/12.7 replaced was the
#:     `.lazyaf-context/step_N.log` CHANNEL, not the directory or its cleanup.
#:     Banning the token would have failed the tier on live behaviour; the
#:     remedy would have been deleting the cleanup, which is a different
#:     decision than this milestone made.

#: Where the grep looks. Deliberately the SHIPPING surface: application code,
#: the frontend, the runner-common package, the operational scripts, the
#: dogfood pipeline definitions and compose. Tests are excluded because this
#: module itself has to name every token.
SEARCH_ROOTS = [
    "backend/app",
    "frontend/src",
    # The browser suite is the one "test" tree that IS shipping surface: its
    # headers and its resetBackend failure message hand an operator a literal
    # `docker compose ... up -d` line. Six of them still named the deleted
    # `runner-mock-e2e` service, because nothing walked here.
    "frontend/e2e",
    "runner-common/runner_common",
    # The agent-side package, listed by its PACKAGE directory rather than by
    # `runner-agent/` so the walk does not descend into its checked-in
    # `.venv`. Same shape as runner-common above.
    "runner-agent/lazyaf_runner",
    # The control runtime that ships INSIDE every step container. Real
    # python, operator-visible, and walked by nothing until 12.8.
    "images/base/control",
    "cli",
    "scripts",
    ".lazyaf",
    # All four compose files, not just the default one. A compose service
    # naming a deleted image is the exact rot section 7.4 found in
    # frontend/e2e, and three of these four were unpoliced.
    "docker-compose.yml",
    "docker-compose.dev.yml",
    "docker-compose.qa.yml",
    "docker-compose.release.yml",
    # Operator manifests. A k8s deployment pointing at a deleted runner image
    # fails at `kubectl apply`, which is the worst place to find out.
    "deploy/k8s",
]

#: DELIBERATELY NOT a search root: `backend/alembic`.
#:
#: A migration is a historical record. `0007`'s docstring explains why it
#: NULLs `executor='legacy'`, `0014`'s explains what the v1 array was and
#: carries a FROZEN COPY of the converter that reads it, and both must keep
#: saying so after the things they name are gone - the chain is the one place
#: in the tree whose job is to remember. It is the same reasoning as the
#: ALLOWLIST below, applied to code rather than prose, and it is recorded here
#: so the next person to notice the gap knows it was a decision.
#:
#: The migrations are not thereby unguarded: `0015`'s behaviour is pinned by
#: `tdd/integration/test_migrations_pipeline_retirement.py`, and that suite
#: asserts the frozen converter stays frozen and imports no application code.

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
# 2b. The v1 array pipeline format - ABSENCE OF SYMBOL (12.8 P6)
# -----------------------------------------------------------------------------
#
# v1 array format, retired 12.8. There is no module to import, so the
# tombstone asserts ABSENCE OF SYMBOL - and, for the fork itself, absence of
# a PARAMETER, which is the only thing that can prove the two-way branch is
# actually gone rather than renamed.
#
# Why the grep above is not enough: it cannot tell a live caller from a
# docstring, and every token it CAN safely ban is one the surviving code does
# not also need. `parse_steps` shares its prefix with the live
# `parse_steps_graph`; `steps` is a substring of four live column names. Those
# are asserted here, by symbol, where a name collision is impossible.
#
# Why the import test is not enough: v1 was never a module. It was a fork
# inside `pipeline_executor`, which survives - so there is nothing to
# `ModuleNotFoundError` on, and a deletion that renamed `_handle_action` to
# `_dispatch_action` would satisfy every other assertion in this file.
#
# WRITTEN IN THE DELETION COMMIT, not after. This module's own docstring
# records what happens otherwise.


def test_the_v1_module_level_vocabulary_is_gone():
    """`parse_steps` and the closed action vocabulary it fed.

    These read the array as an instruction to the RUN. A graph reads FLOW off
    its edges and EFFECT off `node.actions`, so there is nothing left for a
    module-level action table to be the source of truth for (R3).
    """
    from app.services import pipeline_executor as pe

    for name in ("parse_steps", "STEP_ACTIONS", "STEP_ACTION_PREFIXES",
                 "describe_step_action", "_ACTION_VOCABULARY"):
        assert not hasattr(pe, name), (
            f"`pipeline_executor.{name}` is back. It belonged to the v1 array "
            "format, which 12.8 retired - flow lives on edges now, and node "
            "effects live in `PipelineStepV2.actions`"
        )

    # The graph replacements, so that deleting all five and adding nothing
    # would not pass. `describe_terminal_action` is the vocabulary that took
    # `describe_step_action`'s place, minus the FLOW words it used to conflate
    # with effects.
    from app.schemas.pipeline import describe_terminal_action  # noqa: F401


def test_the_v1_executor_methods_are_gone():
    """The array fork's own handlers.

    `_execute_graph_step` and `_handle_graph_step_complete` are in this list
    because they were RENAMED to `_execute_step` / `_handle_step_complete`
    when their v1 twins died: there is one path now, so the qualifier that
    distinguished it from the other one has no meaning. A `_execute_graph_step`
    reappearing means a second path reappeared with it.
    """
    from app.services.pipeline_executor import PipelineExecutor

    for name in ("_execute_graph_step", "_handle_graph_step_complete",
                 "_handle_action", "_trigger_pipeline",
                 "_fail_run_on_undispatchable_action"):
        assert not hasattr(PipelineExecutor, name), (
            f"`PipelineExecutor.{name}` is back - the v1 array fork has been "
            "re-opened"
        )


def test_no_method_takes_an_is_graph_parameter():
    """The fork, not its name.

    Every assertion above can be satisfied by a rename. This one cannot: a
    two-way branch has to be told WHICH way, and `is_graph` threaded through
    `_run_executor_step` / `_finish_local_step*` / `_load_local_step_context`
    is what carried that. Renaming the flag does not help - the point is that
    every pipeline is a graph, so no method needs to be told.
    """
    from app.services.pipeline_executor import PipelineExecutor

    offenders = []
    for name, fn in inspect.getmembers(PipelineExecutor, inspect.isfunction):
        try:
            parameters = inspect.signature(fn).parameters
        except (TypeError, ValueError):  # pragma: no cover - builtins
            continue
        if "is_graph" in parameters:
            offenders.append(name)

    assert not offenders, (
        f"{offenders} still take an `is_graph` parameter. Every pipeline is a "
        "graph after 12.8; a method that has to be told which format it is "
        "running is a method with two formats to run"
    )


def test_pipelines_table_has_no_steps_column():
    """The database half, and the reason `steps` is not in FORBIDDEN.

    Migration 0015 dropped the column and `Pipeline.steps` left the model in
    the same commit - they had to, because the column was `nullable=False`
    with no server_default, so either half alone is a backend that cannot
    INSERT a pipeline. Asserting the ORM mapping catches both directions.
    """
    from app.models.pipeline import Pipeline

    assert not hasattr(Pipeline, "has_graph_definition"), (
        "`Pipeline.has_graph_definition()` is back. It answered 'is this row "
        "v2?', a question with two answers only while there were two formats. "
        "Ask `steps_graph` directly - and mind that a row holding '' has no "
        "graph however NOT NULL the old column looked"
    )

    columns = {column.key for column in sa_inspect(Pipeline).columns}
    assert "steps" not in columns, (
        "`pipelines.steps` is back on the model. If a revision re-added the "
        "column, that is a second source of truth for the step list (R3); if "
        "only the model declares it, every pipeline query fails on a column "
        "the schema does not have"
    )
    assert "steps_graph" in columns, (
        "the positive half: deleting `steps` and replacing it with nothing "
        "would pass the assertion above"
    )


def test_the_wire_does_not_carry_the_array_either():
    """A column can be dropped while the API still serves the field from
    somewhere else. `PipelineRead` is the one shape every client reads."""
    from app.schemas.pipeline import PipelineRead

    assert "steps" not in PipelineRead.model_fields
    assert "steps_graph" in PipelineRead.model_fields


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
