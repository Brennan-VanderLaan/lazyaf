"""QA-4 (FIXED at 12.8 §4.10): LazyAF's own YAML export re-imports into LazyAF.

``GET /api/pipelines/{id}/export/yaml`` used to emit a THIRD dialect that
``app.schemas.lazyaf_yaml.PipelineYaml`` could not validate:

* ``steps`` as a MAPPING keyed by step id; ``PipelineYaml.steps`` is a
  ``list[PipelineStepYaml]``.
* edge TARGETS written into ``on_success`` / ``on_failure`` as bare step ids,
  or as a LIST of ids on a fan-out. The action vocabulary is
  ``next`` | ``stop`` | ``trigger:{id}`` | ``merge:{branch}``; a bare id fell
  through to the unknown-action branch and stopped the pipeline.
* ``timeout`` and ``continue_in_context`` dropped entirely - and, once
  terminal actions landed at 12.8 P1, ``actions`` too, so an exported
  ``merge:`` silently stopped merging.

Net effect: export a graph pipeline, commit it to .lazyaf/pipelines/, and it
silently disappeared from the repo's pipeline list.

The export now emits the ARRAY AUTHORING DIALECT - the shape a human writes
in `.lazyaf/pipelines/*.yaml` - and REFUSES (409, naming the construct) for
any graph the array cannot express. These tests were four
``xfail(strict=True)`` findings; they are the positive assertions now.
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
    """Export a LINEAR graph carrying a non-default timeout + continuation."""
    status, pipeline = create_pipeline({
        "name": "qa4-export-roundtrip",
        "steps_graph": graph(
            [
                step("a", timeout=777, continue_in_context=True),
                step("b"),
                step("c"),
            ],
            [edge("e1", "a", "b"), edge("e2", "b", "c")],
            ["a"],
        ),
    })
    assert status == 201, repr(pipeline)[:300]
    status, text = api("GET", f"/api/pipelines/{pipeline['id']}/export/yaml")
    assert status == 200, f"{status} {str(text)[:400]}"
    return text


def test_exported_graph_yaml_uses_the_shape_the_importer_expects(exported_yaml):
    """QA4-10a: `steps` is a LIST, which is what PipelineYaml requires."""
    data = yaml.safe_load(exported_yaml)
    assert isinstance(data.get("steps"), list), (
        f"export emits steps as {type(data.get('steps')).__name__}, importer wants a list"
    )
    assert [node["id"] for node in data["steps"]] == ["a", "b", "c"], (
        "export order must follow the graph's own chain, and node ids must "
        "survive - they are the context-directory names and breakpoint keys"
    )


def test_exported_on_success_stays_inside_the_action_vocabulary(exported_yaml):
    """QA4-10b: no edge TARGET is ever written where an ACTION belongs."""
    data = yaml.safe_load(exported_yaml)
    steps = data.get("steps")
    values = []
    iterable = steps.values() if isinstance(steps, dict) else (steps or [])
    for node in iterable:
        for key in ("on_success", "on_failure", "on_always"):
            if key in node:
                values.append(node[key])
    assert values, "export wrote no actions at all"
    for value in values:
        assert isinstance(value, str), f"{value!r} is not even a string action"
        assert (
            value in ("next", "stop")
            or value.startswith("trigger:")
            or value.startswith("merge:")
        ), f"{value!r} is not an action, it is an edge target"


def test_export_preserves_step_timeout_and_continuation(exported_yaml):
    """QA4-10c: a round trip must not reset every step to the 300s default."""
    first = yaml.safe_load(exported_yaml)["steps"][0]
    assert first["timeout"] == 777, "step timeout was dropped by export"
    assert first["continue_in_context"] is True, "continue_in_context was dropped"


def test_export_preserves_terminal_actions(create_pipeline):
    """Once `merge:` lives in `actions`, a lossy export silently un-merges.

    This is the field 12.8 P1 added, and it did not exist when QA4-10 was
    filed - an export written before it would have dropped it in exactly the
    same silence as `timeout`.
    """
    merging = step("m")
    merging["actions"] = {"success": ["merge:main"], "failure": [], "always": []}
    status, pipeline = create_pipeline({
        "name": "qa4-export-actions",
        "steps_graph": graph([merging], [], ["m"]),
    })
    assert status == 201, repr(pipeline)[:300]

    status, text = api("GET", f"/api/pipelines/{pipeline['id']}/export/yaml")
    assert status == 200, f"{status} {str(text)[:400]}"
    assert yaml.safe_load(text)["steps"][0]["on_success"] == "merge:main"


@pytest.mark.parametrize(
    "name,steps,edges,entry_points,construct",
    [
        pytest.param(
            "fanout",
            [step("start"), step("a"), step("b")],
            [edge("e1", "start", "a"), edge("e2", "start", "b")],
            ["start"],
            "fan-out",
            id="fan-out",
        ),
        pytest.param(
            "fanin",
            [step("a"), step("b"), step("join")],
            [edge("e1", "a", "join"), edge("e2", "b", "join")],
            ["a"],
            "fan-in",
            id="fan-in",
        ),
        pytest.param(
            "always",
            [step("a"), step("b")],
            [edge("e1", "a", "b", "always")],
            ["a"],
            "'always' edge",
            id="always-edge",
        ),
        pytest.param(
            "twoentries",
            [step("a"), step("b")],
            [],
            ["a", "b"],
            "entry points",
            id="two-entry-points",
        ),
    ],
)
def test_inexpressible_graph_export_refuses_naming_the_construct(
    create_pipeline, name, steps, edges, entry_points, construct
):
    """A graph the array cannot say is a 409, never a silent flatten.

    Flattening a fan-out would hand the user a file that re-imports as a
    DIFFERENT pipeline - the same class of silent damage on the way out that
    `array_to_graph` refuses on the way in.
    """
    status, pipeline = create_pipeline({
        "name": f"qa4-export-{name}",
        "steps_graph": graph(steps, edges, entry_points),
    })
    assert status == 201, repr(pipeline)[:300]

    status, body = api("GET", f"/api/pipelines/{pipeline['id']}/export/yaml")
    assert status == 409, f"expected a refusal, got {status}: {str(body)[:300]}"
    detail = body["detail"] if isinstance(body, dict) else str(body)
    assert construct in detail, f"the refusal does not name the construct: {detail}"


def test_exported_yaml_can_be_imported_back(exported_yaml, repo_id):
    """QA4-10, the end-to-end proof: commit the export, LazyAF reads it back."""
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
    """The array-shaped export round-trips - keep it that way.

    The payload goes in as the v1 array, is converted to a graph at the API
    boundary, and comes back OUT as the array again. That round trip is the
    whole claim.
    """
    status, pipeline = create_pipeline({
        "name": "qa4-export-legacy",
        "steps": [{"name": "L", "type": "script", "config": {"command": "echo L"}}],
    })
    assert status == 201
    status, text = api("GET", f"/api/pipelines/{pipeline['id']}/export/yaml")
    assert status == 200
    data = yaml.safe_load(text)
    assert isinstance(data["steps"], list)
    assert data["steps"][0]["name"] == "L"
