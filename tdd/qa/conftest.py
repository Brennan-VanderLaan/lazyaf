"""Fixtures for the QA-4 (resource abuse / pipeline-graph pathology) lane.

Pure-HTTP helpers live in qa4_support.py next to this file; only fixtures
live here. tdd/qa is a package (another QA lane added __init__.py), so plain
`import qa4_support` needs this directory on sys.path explicitly.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_support import (  # noqa: E402
    BASE_URL,
    QA_REPO_NAME,
    api,
    chain_graph,
    dead_step,
    edge,
    graph,
    step,
)

# ---------------------------------------------------------------------------
# Repo fixtures
# ---------------------------------------------------------------------------

def _find_or_ingest_repo() -> str:
    status, body = api("GET", "/api/repos")
    if status == 200 and isinstance(body, list):
        for repo in body:
            if repo.get("name") == QA_REPO_NAME and repo.get("is_ingested"):
                return repo["id"]
    status, body = api("POST", "/api/repos/ingest", {"name": QA_REPO_NAME, "default_branch": "main"})
    assert status == 201, f"could not ingest QA repo: {status} {body}"
    return body["id"]


@pytest.fixture()
def repo_id() -> str:
    """An ingested repo id, re-created if a sibling QA lane reset the stack.

    Function-scoped on purpose: the sandbox is shared and resets are frequent.
    """
    return _find_or_ingest_repo()


@pytest.fixture()
def create_pipeline(repo_id):
    """Factory: create_pipeline({...}) -> (status, body), retrying past resets."""

    def _create(payload: dict, _repo_id=repo_id):
        status, body = api("POST", f"/api/repos/{_repo_id}/pipelines", payload)
        if status == 404:  # stack was reset between fixture setup and now
            _repo_id = _find_or_ingest_repo()
            status, body = api("POST", f"/api/repos/{_repo_id}/pipelines", payload)
        return status, body

    return _create


def _git(*args, cwd=None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture()
def seeded_repo_id(repo_id):
    """A repo that has a real commit on `main`, so workspace population works.

    Steps cannot run against an empty repo (workspace population fails), so
    every test that needs a container to actually start depends on this.
    Skips when git is unavailable or the push fails.
    """
    if shutil.which("git") is None:
        pytest.skip("git not available; cannot seed a repo with a commit")

    status, body = api("GET", f"/api/repos/{repo_id}/branches", timeout=30)
    if status == 200 and isinstance(body, dict) and body.get("branches"):
        return repo_id

    workdir = tempfile.mkdtemp(prefix="lazyaf-qa4-")
    try:
        _git("init", "-q", "-b", "main", cwd=workdir)
        with open(os.path.join(workdir, "README.md"), "w") as handle:
            handle.write("qa4 seed\n")
        _git("add", "-A", cwd=workdir)
        commit = _git(
            "-c", "user.email=qa4@lazyaf.test", "-c", "user.name=qa4",
            "commit", "-qm", "qa4 seed", cwd=workdir,
        )
        if commit.returncode != 0:
            pytest.skip(f"could not create seed commit: {commit.stderr}")
        push = _git("push", "-q", f"{BASE_URL}/git/{repo_id}.git", "main", cwd=workdir)
        if push.returncode != 0:
            pytest.skip(f"could not push seed commit: {push.stderr}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return repo_id
