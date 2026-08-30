"""Docker orchestrator - the only one shipped in 12.6.

Reproduces LocalExecutor's control-mode sequence on a host the backend does not
own: ``create -> put_archive(control_files) -> start``. Mounts are bound at
create, so the workspace volume is addressable before the runtime boots and the
step JWT never appears in ``docker inspect`` env.

Explicitly NOT reproduced from failure_01's agent:

* ``network_mode="host"`` on spawned containers. This attaches to a CONFIGURED
  network (``LAZYAF_STEP_NETWORK``, default ``bridge``). Handing every step the
  host's network namespace on a machine the backend does not own is not a
  default anyone chose; it was a workaround for the backend URL problem, and
  the actual fix is ``LAZYAF_STEP_BACKEND_URL``.
* ``list(coroutine)`` as a log reader.

The structural log-ordering rule (section 7.2) is enforced by the SHAPE of
``run_step``: ``on_log`` is called only in the pre-start phase and in the
post-exit phase, with nothing but ``docker`` calls in between. The step
container reports its own logs over HTTP; if the two streams could overlap the
merged log would read out of order.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Sequence

import docker
import docker.errors
from docker.types import Mount

# Module-level, deliberately: these calls run inside ``asyncio.to_thread``, and
# a lazy import competing for the GIL with a live event loop cost ~2.8s on the
# first step. This file is the docker-SPECIFIC orchestrator; the seam that must
# stay import-clean is orchestrator/base.py + types.py, guarded by
# tests/test_orchestrator_seam.py.

from ..config import RunnerConfig
from ..control_archive import build_control_archive, control_files_to_entries
from ..types import LogSink, MountRejected, StepAssignment, StepOutcome
from ..workspace import DockerWorkspaceProvisioner, WorkspaceError
from .base import OrchestratorUnavailable, StepOrchestrator

logger = logging.getLogger(__name__)

#: Docker SDK client timeout. The 12.3 lesson: a DooD image pull or a busy
#: daemon routinely exceeds the SDK's 60s default, and a timed-out SDK call
#: looks exactly like a hung step.
DOCKER_CLIENT_TIMEOUT = 600

#: Exit codes the agent synthesizes when the container itself never produced
#: one. 143 is pinned by the spec (section 4.4); the other two follow the
#: usual shell conventions so an operator reading a step result recognizes them.
EXIT_AGENT_ERROR = 1     # the agent failed before/around the container
EXIT_TIMEOUT = 124       # `timeout(1)`'s code
EXIT_CANCELLED = 143     # 128 + SIGTERM

#: How often the wait loop re-checks while a container runs.
WAIT_POLL_SECONDS = 1.0

CONTAINER_LABEL_EXECUTION_KEY = "lazyaf.execution_key"
CONTAINER_LABEL_PIPELINE_RUN = "lazyaf.pipeline_run_id"
CONTAINER_LABEL_RUNNER = "lazyaf.runner_id"


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return "timeout" in text or "timed out" in text


class DockerOrchestrator(StepOrchestrator):
    """Executes steps as containers on this host's docker daemon."""

    name = "docker"

    def __init__(
        self,
        config: RunnerConfig,
        *,
        client=None,
        provisioner=None,
    ) -> None:
        self._config = config
        self._client = client
        self._provisioner = provisioner
        self._missing_images: tuple[str, ...] = ()
        self._network_ready: set[str] = set()
        #: execution_key -> container, so cancel can reach a live container
        #: even if the run task is stuck in a docker call.
        self._running: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def preflight(self) -> None:
        """Fail fast and actionably when this host cannot run steps."""
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_client)
        try:
            await asyncio.to_thread(self._client.ping)
        except Exception as exc:
            raise OrchestratorUnavailable(
                "docker daemon unreachable from this runner: "
                f"{exc}. Check that the daemon is running and that this process "
                "can read the socket (on a container runner, mount "
                "/var/run/docker.sock and join its group)."
            ) from exc

        if self._provisioner is None:
            self._provisioner = DockerWorkspaceProvisioner(
                self._client, network=self._config.step_network
            )

        # Images are a WARNING, never a refusal: a host that is merely stale
        # can still run steps whose images it does have, and reporting the
        # staleness as a LABEL puts it in the runner list instead of in a step
        # failure ten minutes later.
        self._missing_images = await asyncio.to_thread(
            self._find_missing_images, self._config.expect_images
        )
        if self._missing_images:
            logger.warning(
                "Runner host is missing expected images %s - advertising "
                "has=[images:stale]",
                ", ".join(self._missing_images),
            )

    def capabilities(self) -> dict:
        has = ["docker"]
        if self._missing_images:
            has.append("images:stale")
        return {"orchestrator": self.name, "has": has}

    async def shutdown(self) -> None:
        if self._provisioner is not None:
            try:
                await asyncio.to_thread(self._provisioner.cleanup_all)
            except Exception:
                logger.warning("Workspace cleanup on shutdown failed", exc_info=True)

    async def cleanup_workspace(self, retain_key: str) -> None:
        if self._provisioner is None:
            return
        try:
            await asyncio.to_thread(self._provisioner.cleanup, retain_key)
        except Exception:
            logger.warning(
                "cleanup_workspace(%s) failed; the idle reaper will catch it",
                retain_key,
                exc_info=True,
            )

    async def reap_idle_workspaces(self) -> list[str]:
        if self._provisioner is None:
            return []
        try:
            return await asyncio.to_thread(self._provisioner.reap_idle)
        except Exception:
            logger.warning("Idle workspace reaper failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def run_step(
        self,
        assignment: StepAssignment,
        *,
        on_log: LogSink,
        cancel: asyncio.Event,
    ) -> StepOutcome:
        container = None
        key = assignment.execution_key

        # ---- PRE-START PHASE: on_log is free here -----------------------
        step_backend_url = self._config.resolve_backend_url(assignment.backend_url)
        clone_url = self._config.resolve_clone_url(
            assignment.clone_url, assignment.repo_id
        )
        # The one line that answers "why can't the step reach the backend"
        # without host access. Three hops now exist (agent->backend WS, step
        # container->backend HTTP, step container->git) and only the first is
        # visible from the backend side.
        on_log(
            [
                f"resolved backend_url={step_backend_url} clone_url={clone_url} "
                f"network={self._config.step_network} volume={assignment.volume} "
                f"image={assignment.image}"
            ]
        )

        try:
            mounts = self._resolve_mounts(assignment)
        except MountRejected as exc:
            on_log([f"ERROR: {exc}"])
            return StepOutcome(EXIT_AGENT_ERROR, f"mount rejected: {exc}")

        if not assignment.volume:
            on_log(["ERROR: assignment carries no workspace volume"])
            return StepOutcome(
                EXIT_AGENT_ERROR, "assignment carries no workspace.volume"
            )

        try:
            on_log([f"provisioning workspace volume {assignment.volume}"])
            cloned = await asyncio.to_thread(
                self._provisioner.ensure_workspace,
                assignment.volume,
                assignment.retain_key,
                clone_url,
                assignment.branch,
                assignment.commit_sha,
            )
            on_log(
                [
                    "workspace ready (cloned)"
                    if cloned
                    else "workspace ready (reused existing checkout)"
                ]
            )
        except WorkspaceError as exc:
            on_log([f"ERROR: {exc}"])
            return StepOutcome(EXIT_AGENT_ERROR, str(exc))
        except Exception as exc:
            on_log([f"ERROR: workspace provisioning failed: {exc}"])
            return StepOutcome(
                EXIT_AGENT_ERROR, f"workspace provisioning failed: {exc}"
            )

        # The agent pulls nothing (section 7.1: build_images.py owns image
        # provenance and tree-hash determinism). A missing image produces the
        # IDENTICAL message the local path produces, so an operator does not
        # have to learn a second vocabulary for the same fault.
        if not await asyncio.to_thread(self._image_present, assignment.image):
            message = f"Image not found: {assignment.image}"
            on_log([f"ERROR: {message}"])
            return StepOutcome(EXIT_AGENT_ERROR, message)

        try:
            await asyncio.to_thread(self._ensure_network, self._config.step_network)
            create_kwargs = self._build_create_kwargs(
                assignment, mounts, step_backend_url
            )
            container = await asyncio.to_thread(
                self._client.containers.create, assignment.image, **create_kwargs
            )
            self._running[key] = container

            if assignment.control_mode:
                archive = build_control_archive(
                    control_files_to_entries(assignment.control_files)
                )
                await asyncio.to_thread(
                    container.put_archive, assignment.mount_path, archive
                )

            on_log([f"starting container for step {assignment.step_id}"])
            if cancel.is_set():
                return StepOutcome(EXIT_CANCELLED, "cancelled before start")

            # ---- START: the log window CLOSES here ----------------------
            # The step's clock starts at start(), never at run_step(): image
            # resolution and workspace cloning on a cold remote host must not
            # eat the step's own timeout budget (section 7.1).
            await asyncio.to_thread(container.start)
            outcome = await self._await_container(container, assignment, cancel)
            # ---- EXIT: the log window REOPENS here ----------------------
        except Exception as exc:  # daemon faults, create failures, bad config
            logger.warning("Step %s failed in the agent", assignment.step_id, exc_info=True)
            on_log([f"ERROR: {exc}"])
            return StepOutcome(EXIT_AGENT_ERROR, str(exc))
        finally:
            self._running.pop(key, None)
            if container is not None:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except Exception:
                    logger.warning(
                        "Failed to remove step container for %s", key, exc_info=True
                    )

        # ---- POST-EXIT PHASE: on_log is free again ----------------------
        if outcome.error:
            on_log([f"step finished with exit {outcome.exit_code}: {outcome.error}"])
        else:
            on_log([f"step finished with exit {outcome.exit_code}"])
        return outcome

    async def _await_container(
        self,
        container,
        assignment: StepAssignment,
        cancel: asyncio.Event,
    ) -> StepOutcome:
        """Wait for the container, honoring cancel and the step deadline.

        NOTHING in here touches ``on_log``: this is the window in which the
        step container is POSTing its own logs to ``/api/steps/{id}/logs``, and
        a runner-origin line landing in the middle of that stream would make
        the merged log read as though events happened out of order.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + assignment.timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._kill(container)
                return StepOutcome(
                    EXIT_TIMEOUT,
                    f"step exceeded its {assignment.timeout}s timeout on the runner",
                )

            slice_seconds = min(WAIT_POLL_SECONDS, remaining)
            wait_task = asyncio.create_task(
                asyncio.to_thread(container.wait, timeout=slice_seconds)
            )
            cancel_task = asyncio.create_task(cancel.wait())
            try:
                done, _ = await asyncio.wait(
                    {wait_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                cancel_task.cancel()

            if wait_task in done:
                try:
                    result = wait_task.result()
                except Exception as exc:
                    if _is_timeout_error(exc):
                        continue  # the slice expired; re-check the deadline
                    raise
                # A non-zero exit is a STEP failure, not an agent error, so
                # `error` stays None: the backend decides what a code means.
                # Only faults the container never got to express (timeout,
                # cancel, agent-side breakage) carry a message.
                return StepOutcome(int((result or {}).get("StatusCode", -1)))

            # Cancelled: kill now, then let the wait settle so the container is
            # genuinely gone before the outcome is reported.
            await self._kill(container)
            try:
                await wait_task
            except Exception:
                pass
            return StepOutcome(EXIT_CANCELLED, "cancelled")

    async def cancel_running(self, execution_key: str) -> bool:
        """Kill a live container out-of-band. Best effort, never raises."""
        container = self._running.get(execution_key)
        if container is None:
            return False
        return await self._kill(container)

    async def _kill(self, container) -> bool:
        try:
            await asyncio.to_thread(container.kill)
            return True
        except Exception:
            logger.debug("Container kill failed (already gone?)", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Sync helpers - always reached through asyncio.to_thread
    # ------------------------------------------------------------------
    def _build_client(self):
        return docker.from_env(timeout=DOCKER_CLIENT_TIMEOUT)

    def _image_present(self, image: str) -> bool:
        try:
            self._client.images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False
        except Exception:
            # A daemon hiccup is not "your image is wrong": let dispatch
            # surface the real fault instead of blaming the tag.
            logger.warning("Image inspection failed for %s", image, exc_info=True)
            return True

    def _find_missing_images(self, images: Sequence[str]) -> tuple[str, ...]:
        return tuple(image for image in images if not self._image_present(image))

    def _ensure_network(self, network_name: str) -> None:
        if network_name in self._network_ready or network_name in ("bridge", "host", "none"):
            self._network_ready.add(network_name)
            return
        try:
            self._client.networks.get(network_name)
        except docker.errors.NotFound:
            logger.info("Creating step network %s on this runner", network_name)
            try:
                self._client.networks.create(network_name, driver="bridge")
            except Exception:
                self._client.networks.get(network_name)  # lost a creation race
        self._network_ready.add(network_name)

    # ------------------------------------------------------------------
    def _resolve_mounts(self, assignment: StepAssignment) -> tuple[dict, list]:
        """Workspace volume + allowlisted step mounts.

        A BIND mount not on this runner's allowlist is refused (R6 and
        section 3.2): a backend must not be able to bind arbitrary host paths
        on a machine it does not own, and "the backend asked for it" is not
        authorization on someone else's hardware.
        """
        volumes: dict = {
            assignment.volume: {"bind": assignment.mount_path, "mode": "rw"}
        }
        binds: list = []
        allowed = set(self._config.bind_allowlist)
        for mount in assignment.mounts:
            if mount.addressing == "volume":
                volumes[mount.source] = {"bind": mount.target, "mode": mount.mode}
                continue
            if mount.source not in allowed:
                raise MountRejected(
                    f"bind mount source {mount.source!r} is not on this runner's "
                    f"allowlist (allowed: {sorted(allowed) or '<none>'}). Set "
                    "LAZYAF_BIND_ALLOWLIST on the runner host to permit it."
                )
            binds.append(mount)
        return volumes, binds

    def _build_create_kwargs(
        self,
        assignment: StepAssignment,
        mounts: tuple[dict, list],
        step_backend_url: str,
    ) -> dict:
        volumes, binds = mounts
        environment = dict(assignment.environment)
        # The container's view of the backend must be the RUNNER's view, not
        # the backend's own compose alias (section 3.4).
        if step_backend_url:
            environment["LAZYAF_BACKEND_URL"] = step_backend_url

        create_kwargs: dict = {
            "command": assignment.command,
            "volumes": volumes,
            "working_dir": assignment.working_dir,
            "environment": environment,
            "network": self._config.step_network,
            "labels": {
                CONTAINER_LABEL_EXECUTION_KEY: assignment.execution_key,
                CONTAINER_LABEL_PIPELINE_RUN: environment.get(
                    "LAZYAF_PIPELINE_RUN_ID", ""
                ),
                CONTAINER_LABEL_RUNNER: self._config.runner_id,
            },
        }
        if binds:
            create_kwargs["mounts"] = [
                Mount(
                    target=mount.target,
                    source=mount.source,
                    type="bind",
                    read_only=(mount.mode == "ro"),
                )
                for mount in binds
            ]
        if assignment.memory_limit:
            create_kwargs["mem_limit"] = assignment.memory_limit
        return create_kwargs


__all__ = [
    "CONTAINER_LABEL_EXECUTION_KEY",
    "CONTAINER_LABEL_PIPELINE_RUN",
    "CONTAINER_LABEL_RUNNER",
    "DOCKER_CLIENT_TIMEOUT",
    "EXIT_AGENT_ERROR",
    "EXIT_CANCELLED",
    "EXIT_TIMEOUT",
    "DockerOrchestrator",
]
