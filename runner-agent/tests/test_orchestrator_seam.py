"""The executor seam is Docker-agnostic, and that is CHECKABLE.

Test contract item 1 (section 8, Agent D). ``orchestrator/base.py`` and
``types.py`` must import nothing from ``docker``, because the owner target for
remote execution is runpod-style nodes that frequently run AS containers with
no Docker socket. "NativeOrchestrator is deferred but not precluded" is only a
real claim if something fails when it stops being true.

These tests are UNCONDITIONAL. No importorskip, no try/except ImportError.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from lazyaf_runner.orchestrator import base as base_module
from lazyaf_runner.orchestrator.base import (
    OrchestratorUnavailable,
    StepOrchestrator,
    merge_labels,
)
from lazyaf_runner.orchestrator.registry import ORCHESTRATORS, build_orchestrator
from lazyaf_runner.types import StepOutcome

from conftest import StubOrchestrator, make_config

PACKAGE_DIR = Path(base_module.__file__).resolve().parents[1]

#: The two files the seam depends on staying import-clean.
SEAM_FILES = [
    PACKAGE_DIR / "orchestrator" / "base.py",
    PACKAGE_DIR / "types.py",
]


def _imported_module_names(path: Path) -> set[str]:
    """Every module name any import statement in ``path`` refers to."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module=None; relative imports cannot be
            # `docker` by construction, but record what there is.
            if node.module:
                names.add(node.module)
    return names


@pytest.mark.parametrize("path", SEAM_FILES, ids=lambda p: p.name)
def test_seam_file_has_no_docker_import(path: Path) -> None:
    """AST-level, so a docker import inside a function body is caught too."""
    assert path.exists(), f"{path} is missing - the seam moved without this test"
    offenders = {
        name
        for name in _imported_module_names(path)
        if name == "docker" or name.startswith("docker.")
    }
    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}. base.py and types.py are the "
        "seam a socketless (runpod-style) host plugs a NativeOrchestrator into; "
        "a docker import here precludes the thing the seam exists for."
    )


@pytest.mark.parametrize("path", SEAM_FILES, ids=lambda p: p.name)
def test_seam_file_imports_only_stdlib_and_siblings(path: Path) -> None:
    """Nothing third-party at all, and nothing from the backend."""
    allowed_stdlib = {"abc", "asyncio", "dataclasses", "typing", "__future__"}
    for name in _imported_module_names(path):
        root = name.split(".")[0]
        if root in allowed_stdlib or name.startswith("lazyaf_runner"):
            continue
        if path.name == "base.py" and name in ("..types", "types"):
            continue
        pytest.fail(
            f"{path.name} imports {name!r}; the seam is stdlib + siblings only "
            "(and must never reach backend/app - a runner host does not have it)"
        )


def test_importing_the_seam_does_not_import_docker() -> None:
    """The package __init__ must not drag the SDK in transitively.

    A re-export of ``docker_orch`` from ``orchestrator/__init__.py`` would make
    ``from lazyaf_runner.orchestrator.base import StepOrchestrator`` pull the
    docker SDK, quietly defeating the AST check above.
    """
    source = (PACKAGE_DIR / "orchestrator" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "docker_orch", (
                "orchestrator/__init__.py re-exports docker_orch, so importing "
                "base.py pulls the docker SDK transitively"
            )


class NativeishOrchestrator(StepOrchestrator):
    """The socketless shape the seam promises, implemented against base.py only."""

    name = "native"

    # The registry calls `cls(config, **kwargs)`; an orchestrator that ignores
    # its config still has to accept it.
    def __init__(self, config=None, **kwargs) -> None:
        self.config = config

    async def preflight(self) -> None:
        return None

    def capabilities(self) -> dict:
        return {"orchestrator": "native", "has": []}

    async def run_step(self, assignment, *, on_log, cancel):
        on_log(["native orchestrator ran the step"])
        return StepOutcome(0)

    async def cleanup_workspace(self, retain_key: str) -> None:
        return None


def test_a_socketless_orchestrator_satisfies_the_abc() -> None:
    orch = NativeishOrchestrator()
    assert isinstance(orch, StepOrchestrator)
    assert orch.capabilities() == {"orchestrator": "native", "has": []}
    # And it advertises no docker, so a step carrying requires:{has:[docker]}
    # can never match it - which is the whole routing story for such a host.
    assert "docker" not in orch.capabilities()["has"]


def test_a_socketless_orchestrator_registers_without_protocol_changes() -> None:
    ORCHESTRATORS["native-test"] = NativeishOrchestrator
    try:
        built = build_orchestrator(make_config(orchestrator="native-test"))
        assert isinstance(built, NativeishOrchestrator)
    finally:
        ORCHESTRATORS.pop("native-test", None)


def test_unknown_orchestrator_is_an_actionable_error() -> None:
    with pytest.raises(OrchestratorUnavailable) as excinfo:
        build_orchestrator(make_config(orchestrator="nope"))
    assert "nope" in str(excinfo.value)
    assert "available" in str(excinfo.value)


def test_abstract_methods_cannot_be_skipped() -> None:
    class Incomplete(StepOrchestrator):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# merge_labels: capabilities feed register.labels
# ---------------------------------------------------------------------------

def test_merge_labels_unions_has_and_prefers_operator_values() -> None:
    merged = merge_labels(
        {"has": ["gpio"], "zone": "workshop", "orchestrator": "mine"},
        {"has": ["docker"], "orchestrator": "docker"},
    )
    assert set(merged["has"]) == {"docker", "gpio"}
    assert merged["zone"] == "workshop"
    # The operator knows their host; the orchestrator only knows itself.
    assert merged["orchestrator"] == "mine"


def test_merge_labels_accepts_scalar_has() -> None:
    merged = merge_labels({"has": "gpio"}, {"has": ["docker"]})
    assert set(merged["has"]) == {"docker", "gpio"}


def test_merge_labels_survives_empty_inputs() -> None:
    assert merge_labels({}, {}) == {}
    assert merge_labels(None, {"orchestrator": "x"}) == {"orchestrator": "x"}


def test_stub_orchestrator_runs_through_the_abc() -> None:
    """Sanity: the suite's own double really satisfies the contract."""
    orch = StubOrchestrator()
    outcome = asyncio.run(
        orch.run_step(
            _dummy_assignment(), on_log=lambda lines: None, cancel=asyncio.Event()
        )
    )
    assert outcome.succeeded


def _dummy_assignment():
    from lazyaf_runner.types import StepAssignment

    return StepAssignment(step_id="s", execution_key="k", config={})
