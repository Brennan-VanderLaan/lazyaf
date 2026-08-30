"""Workspace provisioning on a host the backend cannot see - section 3.4.

Test contract item 8 (section 8, Agent D). A remote host has no view of the
backend's ``lazyaf-workspaces`` volume, so the agent makes its own: get-or-
create the named volume, clone into it if it is empty, reuse it for every step
of the run, and reap it when the run ends.

The clone script is compared against the backend's ``_build_clone_script``
line-for-line, because the two must agree on uid/gid handover: the helper runs
as root and the step runs as 1000, and a missing chown makes every FIRST step
on every fresh remote host fail on write.
"""
from __future__ import annotations

import ast
import time

import pytest

from lazyaf_runner.workspace import (
    CREATED_AT_LABEL,
    RETAIN_KEY_LABEL,
    VOLUME_LABEL,
    WORKSPACE_IDLE_REAP_SECONDS,
    WORKSPACE_STEP_GID,
    WORKSPACE_STEP_UID,
    DockerWorkspaceProvisioner,
    WorkspaceError,
    build_clone_script,
    build_probe_script,
    volume_age_seconds,
    volume_labels,
)

from conftest import REPO_ROOT, FakeDockerClient

POPULATION = REPO_ROOT / "backend" / "app" / "services" / "workspace" / "population.py"


def provisioner(client: FakeDockerClient) -> DockerWorkspaceProvisioner:
    return DockerWorkspaceProvisioner(client, network="bridge")


# ---------------------------------------------------------------------------
# The clone script
# ---------------------------------------------------------------------------

def test_clone_script_shape() -> None:
    script = build_clone_script("http://backend:8000/git/r1.git", "main", None)
    lines = script.splitlines()
    assert lines[0] == "set -e"
    assert "git clone --branch main -- http://backend:8000/git/r1.git /workspace/repo" in script
    assert lines[-1] == f"chown -R {WORKSPACE_STEP_UID}:{WORKSPACE_STEP_GID} /workspace/repo"
    assert "git checkout" not in script


def test_clone_script_detaches_onto_a_commit() -> None:
    script = build_clone_script("http://x/y.git", "main", "2a513dd4")
    assert "git checkout --detach 2a513dd4" in script
    # Order matters: detach happens before the ownership handover.
    assert script.index("git checkout") < script.index("chown")


def test_clone_script_quotes_every_interpolated_value() -> None:
    """A branch name is attacker-adjacent input on a shared backend."""
    script = build_clone_script("http://x/y.git", "main; rm -rf /", None)
    assert "; rm -rf /" not in script.replace("'main; rm -rf /'", "")
    assert "'main; rm -rf /'" in script


def test_clone_script_matches_the_backends_chown_contract() -> None:
    """Read out of the backend's own source, so a uid change there fails here.

    The one thing the two clone scripts MUST agree on is the ownership
    handover: the helper runs as root and the step runs as 1000. failure_01's
    remote path had no chown at all, so the first step on every fresh remote
    host died on write.
    """
    assert POPULATION.exists(), "backend workspace population module moved"
    tree = ast.parse(POPULATION.read_text(encoding="utf-8"), filename=str(POPULATION))
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "WORKSPACE_STEP_UID",
                    "WORKSPACE_STEP_GID",
                ):
                    constants[target.id] = ast.literal_eval(node.value)
    assert constants == {
        "WORKSPACE_STEP_UID": WORKSPACE_STEP_UID,
        "WORKSPACE_STEP_GID": WORKSPACE_STEP_GID,
    }, "the agent and the backend disagree about the step uid/gid"


def test_probe_script_checks_dot_git_not_the_directory() -> None:
    """A clone that died half-way leaves `repo/` behind; treating that as
    populated hands every later step a broken tree with no way to recover."""
    assert build_probe_script() == "test -d /workspace/repo/.git"


# ---------------------------------------------------------------------------
# get-or-create
# ---------------------------------------------------------------------------

def test_ensure_volume_creates_then_reuses() -> None:
    client = FakeDockerClient()
    prov = provisioner(client)

    assert prov.ensure_volume("v1", "run1") is False  # created
    assert prov.ensure_volume("v1", "run1") is True   # reused
    assert client.event_names().count("volume.create") == 1


def test_created_volume_carries_identifying_labels() -> None:
    client = FakeDockerClient()
    provisioner(client).ensure_volume("v1", "run1")

    labels = client.volumes.store["v1"].attrs["Labels"]
    assert labels[VOLUME_LABEL] == "true"
    assert labels[RETAIN_KEY_LABEL] == "run1"
    assert labels[CREATED_AT_LABEL]


def test_ensure_workspace_clones_once_and_never_again() -> None:
    """Step 2..N of a run must not re-clone: that is what makes the shared
    workspace (and HOME persistence) work remotely the way it does locally."""
    client = FakeDockerClient()
    prov = provisioner(client)

    assert prov.ensure_workspace("v1", "run1", "http://x/y.git", "main", None) is True
    first_helpers = client.event_names().count("container.create")

    assert prov.ensure_workspace("v1", "run1", "http://x/y.git", "main", None) is False
    # Exactly one more helper container: the probe. No second clone.
    assert client.event_names().count("container.create") == first_helpers + 1


def test_an_unpopulated_existing_volume_is_cloned() -> None:
    """An empty volume left over from a failed run must not be trusted."""
    client = FakeDockerClient()
    prov = provisioner(client)
    prov.ensure_volume("v1", "run1")

    client.helper_exit_code = 1  # the probe reports "no .git"
    calls: list = []
    original = DockerWorkspaceProvisioner.populate
    DockerWorkspaceProvisioner.populate = lambda self, *a: calls.append(a)  # type: ignore[assignment]
    try:
        assert prov.ensure_workspace("v1", "run1", "http://x/y.git", "main", None) is True
    finally:
        DockerWorkspaceProvisioner.populate = original  # type: ignore[assignment]
    assert calls, "an existing but empty volume was not populated"


def test_clone_failure_raises_with_the_helper_log_tail() -> None:
    client = FakeDockerClient()
    client.helper_exit_code = 128
    client.helper_logs = b"fatal: could not read Username for 'http://backend:8000'"
    prov = provisioner(client)

    with pytest.raises(WorkspaceError) as excinfo:
        prov.populate("v1", "http://backend:8000/git/r1.git", "main", None)
    message = str(excinfo.value)
    assert "could not read Username" in message, "the log tail is the whole point"
    assert "http://backend:8000/git/r1.git" in message
    assert "main" in message


def test_helper_container_is_always_removed_even_on_failure() -> None:
    client = FakeDockerClient()
    client.helper_exit_code = 1
    prov = provisioner(client)
    with pytest.raises(WorkspaceError):
        prov.populate("v1", "http://x/y.git", "main", None)
    assert client.containers.created[-1].removed


def test_helper_that_never_finishes_is_killed() -> None:
    client = FakeDockerClient()

    def hang(container):
        raise TimeoutError("read timed out")

    client.helper_wait_hook = hang
    prov = provisioner(client)
    with pytest.raises(WorkspaceError) as excinfo:
        prov.populate("v1", "http://x/y.git", "main", None)
    assert client.containers.created[-1].killed
    assert "did not finish" in str(excinfo.value)


def test_helper_mounts_the_volume_at_the_workspace_root() -> None:
    client = FakeDockerClient()
    provisioner(client).populate("v1", "http://x/y.git", "main", None)
    kwargs = client.containers.created[-1].kwargs
    assert kwargs["volumes"] == {"v1": {"bind": "/workspace", "mode": "rw"}}
    assert kwargs["network"] == "bridge"
    assert kwargs["labels"]["lazyaf.volume"] == "v1"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def test_cleanup_removes_only_the_named_retain_key() -> None:
    """Scoped by retain_key, never a label sweep: a runner shares its host with
    whatever else the operator runs, and a broad prune turns a cleanup message
    into a data-loss incident."""
    client = FakeDockerClient()
    prov = provisioner(client)
    prov.ensure_volume("v-run1", "run1")
    prov.ensure_volume("v-run2", "run2")

    prov.cleanup("run1")

    assert "v-run1" not in client.volumes.store
    assert "v-run2" in client.volumes.store


def test_cleanup_of_an_unknown_retain_key_is_silent() -> None:
    client = FakeDockerClient()
    prov = provisioner(client)
    prov.ensure_volume("v-run1", "run1")

    prov.cleanup("never-heard-of-it")

    assert "v-run1" in client.volumes.store
    assert "volume.remove" not in client.event_names()


def test_cleanup_is_idempotent() -> None:
    client = FakeDockerClient()
    prov = provisioner(client)
    prov.ensure_volume("v1", "run1")
    prov.cleanup("run1")
    prov.cleanup("run1")  # must not raise
    assert client.event_names().count("volume.remove") == 1


def test_cleanup_all_drains_everything_this_process_made() -> None:
    client = FakeDockerClient()
    prov = provisioner(client)
    prov.ensure_volume("v1", "r1")
    prov.ensure_volume("v2", "r2")

    removed = prov.cleanup_all()

    assert sorted(removed) == ["v1", "v2"]
    assert client.volumes.store == {}


# ---------------------------------------------------------------------------
# The idle reaper
# ---------------------------------------------------------------------------

def test_volume_age_reads_the_created_at_label() -> None:
    labels = volume_labels("run1")
    age = volume_age_seconds({"Labels": labels})
    assert age is not None and 0 <= age < 5


def test_volume_age_is_none_without_a_label() -> None:
    assert volume_age_seconds({"Labels": {}}) is None
    assert volume_age_seconds({}) is None
    assert volume_age_seconds({"Labels": {CREATED_AT_LABEL: "not-a-date"}}) is None


def test_idle_reaper_removes_only_old_volumes() -> None:
    """The backstop for a backend that crashed without sending
    cleanup_workspace - a runner host must not fill up forever."""
    client = FakeDockerClient()
    prov = provisioner(client)
    prov.ensure_volume("fresh", "r-fresh")
    prov.ensure_volume("stale", "r-stale")

    old = time.time() - (WORKSPACE_IDLE_REAP_SECONDS + 60)
    from datetime import datetime, timezone

    client.volumes.store["stale"].attrs["Labels"][CREATED_AT_LABEL] = datetime.fromtimestamp(
        old, timezone.utc
    ).isoformat()

    removed = prov.reap_idle()

    assert removed == ["stale"]
    assert "fresh" in client.volumes.store


def test_idle_reaper_forgets_volumes_that_vanished() -> None:
    client = FakeDockerClient()
    prov = provisioner(client)
    prov.ensure_volume("gone", "r1")
    del client.volumes.store["gone"]

    assert prov.reap_idle() == []
    # And it stops tracking it, so cleanup later is a no-op rather than an error.
    prov.cleanup("r1")
