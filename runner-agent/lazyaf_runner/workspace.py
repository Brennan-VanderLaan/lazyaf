"""Workspace provisioning on a remote host - Phase 12.6, section 3.4.

A remote host cannot see the backend's ``lazyaf-workspaces`` volume, so the
AGENT provisions its own from ``execute_step.config.workspace``:

1. get-or-create the named volume ``workspace.volume`` on this host's daemon;
2. if ``/workspace/repo`` is absent, clone ``workspace.clone_url`` at
   ``branch`` and check out ``commit_sha`` detached when given;
3. key everything by ``workspace.retain_key`` (= ``pipeline_run_id``) so every
   step of a run reuses one volume and ``HOME=/workspace/home`` persistence
   works remotely exactly as it does locally;
4. reap on ``cleanup_workspace{retain_key}``, on ``drain``, and via an idle
   reaper as the backstop for a backend that never sent the message.

The clone script is REIMPLEMENTED here rather than imported: the backend's
``services/workspace/population.py`` is ~60 lines around one shell string and
importing it would drag ``backend/app`` onto every runner host. The script's
shape is asserted by ``tests/test_workspace.py``.

The pure functions at the top take no docker client and are the part worth
unit-testing without a daemon; ``DockerWorkspaceProvisioner`` is the thin
daemon-touching shell around them, driven in tests through an injected fake
client.
"""
from __future__ import annotations

import logging
import shlex
import time
from datetime import datetime, timezone

import docker.errors

# Imported at MODULE level, deliberately. These calls run inside
# ``asyncio.to_thread``, and a lazy `import docker.errors` in there means a
# heavyweight import competing for the GIL with a live event loop - measured at
# ~2.9s on the first step versus ~0.1s when the import is already done. A
# workspace provisioner is docker-specific by definition, so there is nothing
# to gain by deferring it. (The DOCKER-AGNOSTIC files are orchestrator/base.py
# and types.py, and tests/test_orchestrator_seam.py guards those.)

logger = logging.getLogger(__name__)

#: uid/gid the step container runs as. The clone helper runs as root, so the
#: cloned tree must be handed over explicitly or every step fails on write.
WORKSPACE_STEP_UID = 1000
WORKSPACE_STEP_GID = 1000

#: Hard ceiling on a clone before the helper is killed.
CLONE_TIMEOUT_SECONDS = 300

#: Backstop reaper for a backend that never sent ``cleanup_workspace``.
WORKSPACE_IDLE_REAP_SECONDS = 3600

#: Image the clone helper runs in (bash + git). Same default as the backend's
#: ``settings.workspace_clone_image``.
DEFAULT_CLONE_IMAGE = "python:3.12"

#: How much helper output rides along in a failure message.
LOG_TAIL_LINES = 50

VOLUME_LABEL = "lazyaf.runner-workspace"
RETAIN_KEY_LABEL = "lazyaf.retain_key"
CREATED_AT_LABEL = "lazyaf.created_at"


class WorkspaceError(RuntimeError):
    """Workspace provisioning failed; the message is meant for a step log."""


# ---------------------------------------------------------------------------
# Pure helpers (no docker)
# ---------------------------------------------------------------------------

def build_clone_script(clone_url: str, branch: str, commit_sha: str | None) -> str:
    """The bash script the clone helper runs.

    Mirrors ``population._build_clone_script``: ``set -e``, clone the branch,
    optionally detach onto ``commit_sha``, then chown the tree to the step
    uid/gid. Every interpolated value is ``shlex.quote``d - a branch name is
    attacker-adjacent input on a shared backend.
    """
    lines = [
        "set -e",
        f"git clone --branch {shlex.quote(branch)} -- "
        f"{shlex.quote(clone_url)} /workspace/repo",
        "cd /workspace/repo",
    ]
    if commit_sha:
        lines.append(f"git checkout --detach {shlex.quote(commit_sha)}")
    lines.append(f"chown -R {WORKSPACE_STEP_UID}:{WORKSPACE_STEP_GID} /workspace/repo")
    return "\n".join(lines)


def build_probe_script() -> str:
    """Exit 0 when the workspace already carries a repo, 1 when it does not.

    ``.git`` specifically, not ``repo/``: a clone that died half-way leaves a
    directory behind, and treating that as populated would give every
    subsequent step a broken tree with no way to recover.
    """
    return "test -d /workspace/repo/.git"


def volume_labels(retain_key: str) -> dict:
    return {
        VOLUME_LABEL: "true",
        RETAIN_KEY_LABEL: retain_key,
        CREATED_AT_LABEL: datetime.now(timezone.utc).isoformat(),
    }


def volume_age_seconds(volume_attrs: dict, *, now: float | None = None) -> float | None:
    """Age of a volume from its ``lazyaf.created_at`` label, or None."""
    labels = (volume_attrs or {}).get("Labels") or {}
    raw = labels.get(CREATED_AT_LABEL)
    if not raw:
        return None
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = time.time() if now is None else now
    return current - created.timestamp()


# ---------------------------------------------------------------------------
# Docker-backed provisioner
# ---------------------------------------------------------------------------

class DockerWorkspaceProvisioner:
    """Named-volume workspace lifecycle on this host's docker daemon.

    Every method here is SYNCHRONOUS and blocking on purpose: the docker SDK
    is sync, and the orchestrator calls into it through ``asyncio.to_thread``.
    Keeping the blocking boundary at one place makes it obvious where the
    event loop is protected.
    """

    def __init__(
        self,
        client,
        *,
        network: str = "bridge",
        clone_image: str = DEFAULT_CLONE_IMAGE,
        clone_timeout: float = CLONE_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._network = network
        self._clone_image = clone_image
        self._clone_timeout = clone_timeout
        #: retain_key -> volume name, for cleanup and the idle reaper.
        self._provisioned: dict[str, str] = {}

    # --- volume ---------------------------------------------------------
    def ensure_volume(self, volume_name: str, retain_key: str) -> bool:
        """Get-or-create ``volume_name``. Returns True when it already existed.

        Idempotent by construction: a second call for the same run is a single
        ``volumes.get`` and nothing else, which is what makes step 2..N of a
        run cheap.
        """
        self._provisioned[retain_key] = volume_name
        try:
            self._client.volumes.get(volume_name)
            return True
        except docker.errors.NotFound:
            pass
        self._client.volumes.create(name=volume_name, labels=volume_labels(retain_key))
        logger.info("Created workspace volume %s (retain_key=%s)", volume_name, retain_key)
        return False

    def is_populated(self, volume_name: str) -> bool:
        """Does the volume already carry a git checkout?"""
        exit_code, _ = self._run_helper(
            ["bash", "-c", build_probe_script()],
            volume_name,
            role="workspace-probe",
            timeout=60,
        )
        return exit_code == 0

    def populate(
        self,
        volume_name: str,
        clone_url: str,
        branch: str,
        commit_sha: str | None,
    ) -> None:
        """Clone into the volume. Raises :class:`WorkspaceError` with a log tail."""
        script = build_clone_script(clone_url, branch, commit_sha)
        exit_code, tail = self._run_helper(
            ["bash", "-c", script],
            volume_name,
            role="workspace-populate",
            timeout=self._clone_timeout,
        )
        if exit_code != 0:
            raise WorkspaceError(
                f"workspace clone into {volume_name!r} failed (exit {exit_code}) "
                f"from {clone_url!r} branch {branch!r} commit "
                f"{commit_sha or '<branch head>'}\n"
                f"--- clone helper log tail ---\n{tail}"
            )

    def ensure_workspace(
        self,
        volume_name: str,
        retain_key: str,
        clone_url: str,
        branch: str,
        commit_sha: str | None,
    ) -> bool:
        """Full get-or-create + populate-if-empty. Returns True if it cloned."""
        existed = self.ensure_volume(volume_name, retain_key)
        if existed and self.is_populated(volume_name):
            return False
        self.populate(volume_name, clone_url, branch, commit_sha)
        return True

    def cleanup(self, retain_key: str) -> None:
        """Remove ONLY the volume provisioned for ``retain_key``. Never raises.

        Scoped by retain_key rather than by label sweep: a runner shares a host
        with whatever else the operator runs, and a broad ``prune`` is how a
        cleanup message becomes a data-loss incident.
        """
        volume_name = self._provisioned.pop(retain_key, None)
        if not volume_name:
            logger.debug("cleanup_workspace: no volume tracked for %s", retain_key)
            return
        self._remove_volume(volume_name)

    def reap_idle(self, *, max_age: float = WORKSPACE_IDLE_REAP_SECONDS) -> list[str]:
        """Remove tracked volumes older than ``max_age``. Returns what went.

        The backstop for a backend that never sent ``cleanup_workspace`` -
        a crashed backend must not leave a runner host filling up forever.
        """
        removed: list[str] = []
        for retain_key, volume_name in list(self._provisioned.items()):
            try:
                volume = self._client.volumes.get(volume_name)
            except docker.errors.NotFound:
                self._provisioned.pop(retain_key, None)
                continue
            except Exception:
                logger.warning("Idle reaper could not inspect %s", volume_name, exc_info=True)
                continue
            age = volume_age_seconds(getattr(volume, "attrs", None) or {})
            if age is not None and age >= max_age:
                self._provisioned.pop(retain_key, None)
                if self._remove_volume(volume_name):
                    removed.append(volume_name)
        return removed

    def cleanup_all(self) -> list[str]:
        """Drain hook: reap everything this process provisioned."""
        removed = []
        for retain_key in list(self._provisioned):
            volume_name = self._provisioned.pop(retain_key)
            if self._remove_volume(volume_name):
                removed.append(volume_name)
        return removed

    # --- internals ------------------------------------------------------
    def _remove_volume(self, volume_name: str) -> bool:
        try:
            self._client.api.remove_volume(volume_name, force=True)
            logger.info("Removed workspace volume %s", volume_name)
            return True
        except docker.errors.NotFound:
            return False
        except Exception:
            logger.warning("Failed to remove workspace volume %s", volume_name, exc_info=True)
            return False

    def _run_helper(
        self,
        command: list[str],
        volume_name: str,
        *,
        role: str,
        timeout: float,
    ) -> tuple[int, str]:
        """Run a short-lived helper container against the workspace volume.

        Always removed in ``finally`` (deferred rather than auto-remove, so a
        failure's log tail survives long enough to be reported).
        """
        container = self._client.containers.run(
            self._clone_image,
            command=command,
            detach=True,
            volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
            network=self._network,
            remove=False,
            labels={"lazyaf.role": role, "lazyaf.volume": volume_name},
        )
        try:
            try:
                result = container.wait(timeout=timeout)
                exit_code = int(result.get("StatusCode", -1))
            except Exception as exc:
                try:
                    container.kill()
                except Exception:
                    pass
                return -1, f"helper did not finish within {timeout}s: {exc}"
            return exit_code, _log_tail(container)
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass


def _log_tail(container) -> str:
    try:
        raw = container.logs(stdout=True, stderr=True, tail=LOG_TAIL_LINES)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    except Exception:
        return "<log tail unavailable>"


__all__ = [
    "CLONE_TIMEOUT_SECONDS",
    "CREATED_AT_LABEL",
    "DEFAULT_CLONE_IMAGE",
    "RETAIN_KEY_LABEL",
    "VOLUME_LABEL",
    "WORKSPACE_IDLE_REAP_SECONDS",
    "WORKSPACE_STEP_GID",
    "WORKSPACE_STEP_UID",
    "DockerWorkspaceProvisioner",
    "WorkspaceError",
    "build_clone_script",
    "build_probe_script",
    "volume_age_seconds",
    "volume_labels",
]
