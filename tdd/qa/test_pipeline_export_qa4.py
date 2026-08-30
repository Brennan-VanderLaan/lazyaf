"""QA-4: LazyAF's own YAML export cannot be re-imported by LazyAF.

``GET /api/pipelines/{id}/export/yaml`` (backend/app/routers/pipelines.py:490)
serialises a v2 graph pipeline into a document that
``app.schemas.lazyaf_yaml.PipelineYaml`` cannot validate:

* ``steps`` is emitted as a MAPPING keyed by step id; PipelineYaml.steps is a
  ``list[PipelineStepYaml]``.
* edge targets are written into ``on_success`` / ``on_failure`` as bare step
  ids, or as a LIST of ids on a fan-out. The action vocabulary is
  ``next`` | ``stop`` | ``trigger:{id}`` | ``merge:{branch}``; a bare id falls
  through ``_handle_action``'s else branch and stops the pipeline.
* ``timeout`` and ``continue_in_context`` are dropped entirely.
* ``entry_points`` and ``version`` are emitted but PipelineYaml has no such
  fields, so they are discarded on the way back in.

Net effect: export a graph pipeline, commit it to .lazyaf/pipelines/, and it
silently disappears from the repo's pipeline list.
"""

import os
import shutil
import subprocess
import tempfile

import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_support import BASE_URL, api, edge, graph, step

pytestmark = pytest.mark.qa4


@pytest.fixture()
def exported_yaml(create_pipeline):
    """Export a fan-out graph carrying a non-default timeout."""
    status, pipeline = create_pipeline({
        "name": "qa4-export-roundtrip",
        "steps_graph": graph(
            [
                step("a", timeout=777, continue_in_context=True),
                step("b"),
                step("c"),
            ],
            [edge("e1", "a", "b"), edge("e2", "a", "c")],
            ["a"],
        ),
    })
    assert status == 201, repr(pipeline)[:300]
    status, text = api("GET", f"/api/pipelines/{pipeline['id']}/export/yaml")
    assert status == 200, f"{status} {str(text)[:200]}"
    return text


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-10a: export emits `steps` as a mapping, but the "
        "importer (PipelineYaml) requires a list. Committing LazyAF's own "
        "export to .lazyaf/pipelines/ makes the pipeline silently vanish from "
        "the repo listing."
    ),
)
def test_exported_graph_yaml_uses_the_shape_the_importer_expects(exported_yaml):
    data = yaml.safe_load(exported_yaml)
    assert isinstance(data.get("steps"), list), (
        f"export emits steps as {type(data.get('steps')).__name__}, importer wants a list"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-10b: export writes edge TARGETS into on_success. "
        "'on_success: [b, c]' is not in the action vocabulary at all, and "
        "'on_success: b' is treated by _handle_action as an unknown action - "
        "which stops the pipeline and reports the step's own verdict."
    ),
)
def test_exported_on_success_stays_inside_the_action_vocabulary(exported_yaml):
    data = yaml.safe_load(exported_yaml)
    steps = data.get("steps")
    values = []
    iterable = steps.values() if isinstance(steps, dict) else (steps or [])
    for node in iterable:
        for key in ("on_success", "on_failure", "on_always"):
            if key in node:
                values.append(node[key])
    for value in values:
        assert isinstance(value, str), f"{value!r} is not even a string action"
        assert (
            value in ("next", "stop")
            or value.startswith("trigger:")
            or value.startswith("merge:")
        ), f"{value!r} is not an action, it is an edge target"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-10c: export drops per-step `timeout` and "
        "`continue_in_context`, so a round-trip silently resets every step to "
        "the 300s default and loses workspace continuation."
    ),
)
def test_export_preserves_step_timeout_and_continuation(exported_yaml):
    assert "777" in exported_yaml, "step timeout was dropped by export"
    assert "continue_in_context" in exported_yaml, "continue_in_context was dropped by export"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-10: the end-to-end proof. Export a graph pipeline, "
        "commit it to .lazyaf/pipelines/, and the repo pipeline listing does "
        "not contain it - the get-one endpoint answers 500 with a pydantic "
        "'Input should be a valid list' error."
    ),
)
def test_exported_yaml_can_be_imported_back(exported_yaml, repo_id):
    if shutil.which("git") is None:
        pytest.skip("git not available")

    workdir = tempfile.mkdtemp(prefix="lazyaf-qa4-export-")
    try:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workdir, timeout=60)
        pipeline_dir = os.path.join(workdir, ".lazyaf", "pipelines")
        os.makedirs(pipeline_dir)
        with open(os.path.join(workdir, "README.md"), "w") as handle:
            handle.write("qa4 export round-trip\n")
        with open(os.path.join(pipeline_dir, "roundtrip.yaml"), "w", newline="\n") as handle:
            handle.write(exported_yaml)
        subprocess.run(["git", "add", "-A"], cwd=workdir, timeout=60)
        commit = subprocess.run(
            ["git", "-c", "user.email=qa4@lazyaf.test", "-c", "user.name=qa4",
             "commit", "-qm", "qa4 export round-trip"],
            cwd=workdir, capture_output=True, text=True, timeout=60,
        )
        if commit.returncode != 0:
            pytest.skip(f"commit failed: {commit.stderr}")
        push = subprocess.run(
            ["git", "push", "-q", f"{BASE_URL}/git/{repo_id}.git", "main"],
            cwd=workdir, capture_output=True, text=True, timeout=180,
        )
        if push.returncode != 0:
            pytest.skip(f"push failed: {push.stderr}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    status, listed = api("GET", f"/api/repos/{repo_id}/lazyaf/pipelines", timeout=180)
    if status != 200 or not isinstance(listed, list):
        pytest.skip(f"listing unavailable: {status}")
    assert "roundtrip.yaml" in {entry["filename"] for entry in listed}, (
        "LazyAF cannot read back the YAML LazyAF just exported; the file is "
        "silently absent from the repo pipeline listing"
    )


def test_legacy_pipeline_export_is_importable(create_pipeline):
    """The legacy (list) export shape does round-trip - keep it that way."""
    status, pipeline = create_pipeline({
        "name": "qa4-export-legacy",
        "steps": [{"name": "L", "type": "script", "config": {"command": "echo L"}}],
    })
    assert status == 201
    status, text = api("GET", f"/api/pipelines/{pipeline['id']}/export/yaml")
    assert status == 200
    data = yaml.safe_load(text)
    assert isinstance(data["steps"], list)
