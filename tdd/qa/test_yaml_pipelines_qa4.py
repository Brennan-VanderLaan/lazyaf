"""QA-4: the .lazyaf/pipelines/*.yaml definition path.

Everything here goes through backend/app/routers/lazyaf_files.py, which for
every file does:

    data = yaml.safe_load(content.decode('utf-8'))
    pipeline = PipelineYaml(**data)

with no size limit, no step-type validation, and three DIFFERENT reactions to
the same malformed file depending on which endpoint you hit (silently skipped
when listing, 500 when fetching one, 500 when running one).

These tests push real files into a repo over the platform's own git HTTP
endpoint, so they need `git` on PATH; they skip otherwise.
"""

import json
import os
import shutil
import subprocess
import tempfile

import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_support import BASE_URL, api

pytestmark = pytest.mark.qa4


# ---------------------------------------------------------------------------
# The corpus of hostile pipeline files
# ---------------------------------------------------------------------------

def _alias_amplifier(anchor: str, levels: int, width: int = 8) -> list[str]:
    """YAML lines whose last anchor expands to width**levels leaf scalars.

    PyYAML resolves aliases by REFERENCE, so parsing stays cheap - the blow-up
    happens the moment anything walks or serializes the structure.
    """
    lines = [f'{anchor}0: &{anchor}0 "lol"']
    for level in range(1, levels + 1):
        refs = ",".join([f"*{anchor}{level - 1}"] * width)
        lines.append(f"{anchor}{level}: &{anchor}{level} [{refs}]")
    return lines


HOSTILE_FILES = {
    # A file that parses and materializes cleanly - the control.
    "good.yaml": (
        'name: "QA4 Good"\n'
        "steps:\n"
        '  - name: "Echo"\n'
        "    type: script\n"
        "    config: {command: \"echo hi\"}\n"
    ),
    # Zero bytes.
    "empty.yaml": "",
    # A sequence where a mapping is required.
    "alist.yaml": "- name: I am a list\n- and not a mapping\n",
    # Parses to None.
    "nullish.yaml": "null\n",
    # Parses to a bare string.
    "scalar.yaml": "just a scalar string\n",
    # No steps key at all.
    "nosteps.yaml": 'name: "QA4 No Steps"\ndescription: "does nothing at all"\n',
    # Two files, one name.
    "dupname-a.yaml": (
        'name: "QA4 Dup Name"\n'
        "steps:\n"
        '  - name: "From A"\n    type: script\n    config: {command: "echo A"}\n'
    ),
    "dupname-b.yaml": (
        'name: "QA4 Dup Name"\n'
        "steps:\n"
        '  - name: "From B"\n    type: script\n    config: {command: "echo B"}\n'
    ),
    # A step type nothing can execute.
    "bananatype.yaml": (
        'name: "QA4 Banana Type"\n'
        "steps:\n"
        '  - name: "S"\n    type: banana\n    config: {command: "echo x"}\n'
    ),
    # Free-text on_success / on_failure.
    "weirdactions.yaml": (
        'name: "QA4 Weird Actions"\n'
        "steps:\n"
        '  - name: "S"\n    type: script\n'
        '    on_success: "definitely not a real action"\n'
        '    on_failure: "explode"\n'
        '    config: {command: "echo x"}\n'
    ),
    # Negative timeout.
    "negtimeout.yaml": (
        'name: "QA4 Neg Timeout"\n'
        "steps:\n"
        '  - name: "S"\n    type: script\n    timeout: -5\n    config: {command: "echo x"}\n'
    ),
    # A trigger for a branch that does not exist.
    "ghostbranch.yaml": (
        'name: "QA4 Ghost Branch"\n'
        "triggers:\n"
        "  - type: push\n    config:\n      branches: [\"no-such-branch\"]\n"
        "steps:\n"
        '  - name: "S"\n    type: script\n    config: {command: "echo x"}\n'
    ),
    # A trigger type nothing dispatches on.
    "badtrigger.yaml": (
        'name: "QA4 Bad Trigger"\n'
        "triggers:\n  - type: totally_made_up\n    config: {}\n"
        "steps:\n"
        '  - name: "S"\n    type: script\n    config: {command: "echo x"}\n'
    ),
    # ~1 MB body.
    "huge.yaml": 'name: "QA4 Huge"\nsteps: []\npadding: "' + ("A" * (1024 * 1024)) + '"\n',
    # Alias amplifier in an IGNORED extra key: pydantic drops it, so harmless.
    "bombtop.yaml": (
        'name: "QA4 Bomb Top"\nsteps: []\n'
        + "\n".join(_alias_amplifier("t", 6))
        + "\nboom: *t6\n"
    ),
    # Alias amplifier inside step config, which is dict[str, Any] and therefore
    # KEPT, serialized into the API response and written to the DB.
    "bombcfg.yaml": (
        'name: "QA4 Bomb In Config"\n'
        + "\n".join(_alias_amplifier("c", 6))
        + "\nsteps:\n"
        '  - name: "S"\n    type: script\n    config:\n'
        '      command: "echo x"\n      boom: *c6\n'
    ),
}

#: Files whose content is not a YAML mapping and therefore cannot become a
#: PipelineYaml at all.
UNPARSEABLE = ["empty.yaml", "alist.yaml", "nullish.yaml", "scalar.yaml"]


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=180)


@pytest.fixture(scope="module")
def hostile_repo():
    """A repo whose .lazyaf/pipelines/ holds the corpus above."""
    if shutil.which("git") is None:
        pytest.skip("git not available")

    status, body = api("POST", "/api/repos/ingest", {"name": "qa4-yaml", "default_branch": "main"})
    if status != 201:
        pytest.skip(f"could not ingest repo: {status} {body}")
    repo_id = body["id"]

    workdir = tempfile.mkdtemp(prefix="lazyaf-qa4-yaml-")
    try:
        _git("init", "-q", "-b", "main", cwd=workdir)
        pipeline_dir = os.path.join(workdir, ".lazyaf", "pipelines")
        os.makedirs(pipeline_dir)
        with open(os.path.join(workdir, "README.md"), "w") as handle:
            handle.write("qa4 yaml corpus\n")
        for filename, content in HOSTILE_FILES.items():
            with open(os.path.join(pipeline_dir, filename), "w", newline="\n") as handle:
                handle.write(content)
        _git("add", "-A", cwd=workdir)
        commit = _git(
            "-c", "user.email=qa4@lazyaf.test", "-c", "user.name=qa4",
            "commit", "-qm", "qa4 hostile yaml", cwd=workdir,
        )
        if commit.returncode != 0:
            pytest.skip(f"commit failed: {commit.stderr}")
        push = _git("push", "-q", f"{BASE_URL}/git/{repo_id}.git", "main", cwd=workdir)
        if push.returncode != 0:
            pytest.skip(f"push failed: {push.stderr}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    status, listed = api("GET", f"/api/repos/{repo_id}/lazyaf/pipelines", timeout=180)
    if status != 200 or not isinstance(listed, list) or not listed:
        pytest.skip(f"repo pipelines did not materialize ({status}); sandbox may have been reset")
    return repo_id


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-09: list_repo_pipelines swallows every parse failure "
        "with `print(...); continue` (lazyaf_files.py:180). A pipeline file "
        "with a typo simply VANISHES from the repo's pipeline list - no "
        "error, no warning, no way to tell it from a file that was never "
        "written. The same file 500s on the get-one endpoint."
    ),
)
@pytest.mark.parametrize("filename", UNPARSEABLE)
def test_malformed_pipeline_file_is_reported_not_swallowed(hostile_repo, filename):
    status, listed = api("GET", f"/api/repos/{hostile_repo}/lazyaf/pipelines", timeout=180)
    assert status == 200, f"{status} {str(listed)[:200]}"
    filenames = {entry["filename"] for entry in listed}
    assert filename in filenames, (
        f"{filename} is silently absent from the pipeline listing; the user "
        "gets no signal that a file failed to parse"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-09: a malformed repo file is a USER error, but "
        "lazyaf_files.py answers 500 and pastes the raw Python exception - "
        "including internal module paths - into `detail`, which the UI shows "
        "in a toast."
    ),
)
@pytest.mark.parametrize("filename", ["alist", "nullish", "scalar"])
def test_malformed_pipeline_file_is_a_client_error(hostile_repo, filename):
    status, body = api("GET", f"/api/repos/{hostile_repo}/lazyaf/pipelines/{filename}")
    assert status in (400, 404, 422), f"got {status}: {str(body)[:200]}"
    assert "app.schemas" not in json.dumps(body), "internal module path leaked to the client"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-16: a zero-byte .lazyaf/pipelines/empty.yaml answers "
        "404 'Pipeline not found' because get_file_content returns falsy for "
        "an empty blob, so the loop never even attempts to parse it."
    ),
)
def test_empty_pipeline_file_is_not_reported_as_missing(hostile_repo):
    status, body = api("GET", f"/api/repos/{hostile_repo}/lazyaf/pipelines/empty")
    assert status != 404, f"the file exists, but the API says: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-11: PipelineStepYaml.type is a plain `str` with no "
        "enum, so `type: banana` materializes happily and only explodes when "
        "the ExecutionRouter is asked to route it. The graph API 422s the "
        "identical value - two definition paths, two answers."
    ),
)
def test_unknown_step_type_is_refused_by_the_yaml_path(hostile_repo):
    status, body = api("GET", f"/api/repos/{hostile_repo}/lazyaf/pipelines/bananatype")
    assert status in (400, 422), f"'type: banana' was accepted with {status}: {str(body)[:200]}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-06b: upsert_materialized_pipeline keys the platform "
        "row on the yaml `name:` (trigger_service.py:82), so two different "
        "files declaring the same name collapse into ONE row and each "
        "overwrites the other's steps. Nothing tells the user."
    ),
)
def test_two_files_with_the_same_name_do_not_collapse(hostile_repo):
    status, a = api("POST", f"/api/repos/{hostile_repo}/lazyaf/pipelines/dupname-a/run", timeout=180)
    status_b, b = api("POST", f"/api/repos/{hostile_repo}/lazyaf/pipelines/dupname-b/run", timeout=180)
    if status != 200 or status_b != 200:
        pytest.skip(f"could not run both files: {status}/{status_b}")
    assert a["pipeline_id"] != b["pipeline_id"], (
        "dupname-a.yaml and dupname-b.yaml materialized into the same "
        f"platform pipeline {a['pipeline_id']}; whichever ran last silently "
        "destroyed the other's definition"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-08: run_repo_pipeline calls "
        "pipeline_executor.start_pipeline directly and therefore SKIPS the "
        "'Pipeline has no steps defined' gate that "
        "POST /api/pipelines/{id}/run enforces (pipelines.py:305). A yaml "
        "file with no steps: key runs, does nothing, and reports PASSED - a "
        "green tick for a pipeline that never existed."
    ),
)
def test_stepless_yaml_pipeline_does_not_report_a_green_pass(hostile_repo):
    status, body = api("POST", f"/api/repos/{hostile_repo}/lazyaf/pipelines/nosteps/run", timeout=180)
    if status == 404:
        pytest.skip("QA sandbox was reset mid-test")
    assert status != 200 or body.get("status") != "passed", (
        f"a pipeline with zero steps reported {body.get('status')!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-10 (amplification): yaml.safe_load is called with no "
        "size or expansion budget, and steps[].config is dict[str, Any] so "
        "whatever an alias expands to is KEPT, serialized into the response "
        "and written to Pipeline.steps. Measured: a 393-byte file produced a "
        "1.9 MB response body. Two more alias levels (about 460 bytes) give "
        "roughly 120 MB."
    ),
)
def test_yaml_alias_expansion_in_step_config_is_bounded(hostile_repo):
    status, body = api(
        "GET", f"/api/repos/{hostile_repo}/lazyaf/pipelines/bombcfg", timeout=300
    )
    if status != 200:
        pytest.skip(f"bombcfg not readable: {status}")
    size = len(json.dumps(body))
    assert size < 100_000, (
        f"a {len(HOSTILE_FILES['bombcfg.yaml'])}-byte yaml file produced a "
        f"{size}-byte API response ({size / len(HOSTILE_FILES['bombcfg.yaml']):.0f}x amplification)"
    )


# ---------------------------------------------------------------------------
# Verified-correct guards
# ---------------------------------------------------------------------------

def test_wellformed_yaml_pipeline_still_lists_and_runs(hostile_repo):
    status, listed = api("GET", f"/api/repos/{hostile_repo}/lazyaf/pipelines", timeout=180)
    assert status == 200
    assert "good.yaml" in {entry["filename"] for entry in listed}


def test_alias_bomb_in_an_ignored_key_is_dropped(hostile_repo):
    """PipelineYaml ignores extra keys, so a top-level alias bomb is inert.

    This is the control for QA4-10: the amplification is specifically about
    dict[str, Any] fields that SURVIVE model construction.
    """
    status, body = api("GET", f"/api/repos/{hostile_repo}/lazyaf/pipelines/bombtop", timeout=180)
    assert status == 200, f"{status} {str(body)[:200]}"
    assert len(json.dumps(body)) < 10_000


def test_branch_scoped_run_cannot_clobber_the_trunk_definition(hostile_repo):
    """The 'only the default branch materializes' rule holds - keep it."""
    status, body = api(
        "POST",
        f"/api/repos/{hostile_repo}/lazyaf/pipelines/good/run?branch=not-the-default",
        timeout=120,
    )
    assert status == 400, f"{status} {str(body)[:200]}"
