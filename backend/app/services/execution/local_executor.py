"""
Local Executor - Docker-based step execution.

Spawns Docker containers directly from the backend via Docker SDK.
Provides:
- Container spawning with proper configuration (image/working_dir/network from
  settings - single-sourced HERE, not in the pipeline executor)
- Workspace volume mounting at /workspace via EXPLICIT mount addressing
  (MountAddressing enum: volume | bind - never inferred from path shape)
- Bind-mount policy: raw bind mounts from pipeline step config are gated by a
  settings-driven allowlist (default: the docker socket only); anything else
  fails the step loudly at dispatch (12.2-INT fix 10)
- Shell-wrapped script commands under `set -e` (docker-py shlex-splits raw
  strings; multiline piped scripts break without a real shell, and without
  set -e only the LAST line's exit code would decide the step)
- HOME pinned to the shared workspace volume so tools installed in one step
  survive to the next (12.3 persistence contract, pulled forward)
- Real-time log streaming without blocking the event loop (R5): a pump thread
  feeds an asyncio queue; all blocking docker SDK calls go through
  run_in_threadpool
- Timeout handling with a hard deadline that fires DURING log streaming
  (kills container after deadline)
- Crash detection
- Idempotent execution (same key = cached result)
- Deterministic container cleanup: the finished container is removed BEFORE
  the result event is yielded, so step completion is synchronous with the
  container being gone - never dependent on generator GC (12.2-INT fix 6)
"""
import asyncio
import io
import json
import logging
import re
import shlex
import tarfile
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncGenerator, Iterable, Sequence, Union

import docker
from docker import DockerClient
from docker.errors import APIError, ContainerError, DockerException, ImageNotFound, NotFound
from docker.types import Mount
import requests.exceptions
from starlette.concurrency import run_in_threadpool

from app.config import get_settings

logger = logging.getLogger(__name__)


# Default image for steps that don't specify one. Sourced from settings
# (step_default_image); module constant kept for import compatibility.
DEFAULT_STEP_IMAGE = get_settings().step_default_image

# The one bind-mount source permitted from pipeline definitions by default
# (12.2-INT fix 10). The `needs: [docker]` step-config sugar translates to
# this mount; Phase 12.4 changes that one site while raw-bind-with-allowlist
# stays the mechanism underneath.
DOCKER_SOCKET_SOURCE = "/var/run/docker.sock"

# Labels stamped on every step container so orphans are findable and tests
# can assert cleanup synchronously with step completion.
CONTAINER_LABEL_EXECUTION_KEY = "lazyaf.execution_key"
CONTAINER_LABEL_PIPELINE_RUN = "lazyaf.pipeline_run_id"

# ---------------------------------------------------------------------------
# Control mode (Phase 12.3). An image DECLARES the in-container control
# runtime by baking this label (lazyaf-base and children) - explicit
# declaration by the image author, never inferred from path/name shape (R6).
# ---------------------------------------------------------------------------
CONTROL_LAYER_LABEL = "lazyaf.control-layer"
#: A label is DECLARED only when its value is exactly this. Presence alone is
#: not a declaration - `LABEL lazyaf.agent-runtime=0` is an image author
#: saying "no".
LABEL_DECLARED_VALUE = "1"

# Where per-step config files land inside the workspace volume. The backend
# writes ONE file per step execution - .control/<step_execution_id>.json -
# via put_archive onto the created-but-not-started container and points the
# runtime at it with the CONFIG_PATH env var (cross-agent contract #1: the
# runtime honors CONFIG_PATH and unlinks exactly that path). Per-step naming
# kills the fan-out collision where two parallel steps of one run clobbered
# each other's step_config.json on the SHARED workspace volume.
CONTROL_CONFIG_DIR = ".control"

# Agent steps (Phase 12.5) carry a SECOND file in the same tar:
# .control/agent.<step_execution_id>.json, announced to the wrapper through
# LAZYAF_AGENT_CONFIG_PATH inside the STEP CONFIG FILE's environment (never
# container env). Two files, not more keys on one: run.py deletes the step
# config BEFORE the command runs (consume-once), so an agent payload carried
# there would be unreadable - and the step JWT / API key must not live in a
# file the wrapper opens.
AGENT_CONFIG_PREFIX = "agent."
AGENT_CONFIG_PATH_ENV = "LAZYAF_AGENT_CONFIG_PATH"

# Usage channel (Phase 12.5 / M13). Provider stamped into the fallback usage
# record run.py POSTs for every control-mode step, agent or not - a script
# step's row is `cost_source="unknown"` on a self-hosted provider, which is a
# recorded fact, not a gap.
DEFAULT_USAGE_PROVIDER = "self-hosted"

# In control mode the executor no longer ships per-line log events (the
# router is the sole log writer, R3); it retains this many trailing stdout
# lines in memory as a forensics tail, surfaced on the result event so the
# finish path can persist them when the step fails or the router wrote
# nothing.
CONTROL_MODE_LOG_TAIL_LINES = 200

# In control mode the runtime enforces `timeout_seconds` in-container
# (graceful SIGTERM -> SIGKILL); the executor's own deadline moves out by
# this grace so it stays purely the backstop for a dead/wedged runtime. Must
# remain below the pipeline executor's LOCAL_STEP_HARD_TIMEOUT_GRACE (120s)
# so the ordering is: in-container timeout < executor backstop < hard
# deadline.
CONTROL_MODE_TIMEOUT_GRACE = 30


def make_docker_client() -> DockerClient:
    """Build a docker client honoring settings.docker_host.

    Cross-file contract #1 (12.2-INT): workspace population imports this so
    every backend-spawned container speaks to the same daemon. Sync (the SDK
    probes the daemon on construction) - call via run_in_threadpool.
    """
    settings = get_settings()
    if settings.docker_host:
        return docker.DockerClient(base_url=settings.docker_host)
    return docker.from_env()


def bind_mount_allowlist() -> tuple[str, ...]:
    """Bind-mount sources pipeline step config may request (fix 10).

    Settings-driven via ``step_bind_mount_allowlist`` when present (config.py
    is owned by a parallel change - read defensively); default is the docker
    socket only. The workspace volume mount is internal and unaffected.
    """
    settings = get_settings()
    configured = getattr(settings, "step_bind_mount_allowlist", None)
    if configured:
        return tuple(configured)
    return (DOCKER_SOCKET_SOURCE,)


# -----------------------------------------------------------------------------
# Explicit mount addressing (R6): volume vs bind is DECLARED, never inferred
# from the shape of the source string. A Windows host path (C:\...) declared
# as BIND goes through the typed docker Mount API; a named volume goes through
# the volumes dict. A path-looking string declared VOLUME fails loudly.
# -----------------------------------------------------------------------------

class MountAddressing(str, Enum):
    """How a mount source is addressed - explicit, never inferred."""
    VOLUME = "volume"
    BIND = "bind"


# Docker named-volume grammar (local driver). Anything else declared VOLUME
# is rejected loudly instead of letting the engine silently bind-mount it.
_VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


@dataclass(frozen=True)
class MountSpec:
    """One container mount with explicit addressing."""
    addressing: MountAddressing
    source: str
    target: str
    mode: str = "rw"  # "rw" | "ro"

    @classmethod
    def from_config(cls, raw: Union["MountSpec", dict]) -> "MountSpec":
        """Parse a mount config dict. The 'addressing' key is REQUIRED."""
        if isinstance(raw, MountSpec):
            return raw
        if not isinstance(raw, dict):
            raise ValueError(f"mount config must be a dict or MountSpec, got {type(raw).__name__}")
        if "addressing" not in raw:
            raise ValueError(
                "mount config requires explicit 'addressing' ('volume' | 'bind') - "
                "addressing is never inferred from path shape"
            )
        try:
            addressing = MountAddressing(raw["addressing"])
        except ValueError:
            raise ValueError(
                f"invalid mount addressing {raw['addressing']!r}: must be 'volume' or 'bind'"
            ) from None
        if "source" not in raw or "target" not in raw:
            raise ValueError("mount config requires 'source' and 'target'")
        return cls(
            addressing=addressing,
            source=raw["source"],
            target=raw["target"],
            mode=raw.get("mode", "rw"),
        )


def validate_step_mounts(
    specs: Sequence[Union[MountSpec, dict]],
    allowlist: Iterable[str],
) -> list[MountSpec]:
    """Gate mounts requested by PIPELINE STEP CONFIG (12.2-INT fix 10).

    Bind mounts are host-root-equivalent power; only sources on the
    (settings-driven) allowlist may come from a pipeline definition. Named
    volumes pass through - they carry no host paths. Raises ValueError with
    a clear config error; the executor fails the step loudly on it.

    The internal workspace volume mount never goes through here.
    """
    allowed = set(allowlist)
    validated: list[MountSpec] = []
    for raw in specs:
        spec = MountSpec.from_config(raw)
        if spec.addressing is MountAddressing.BIND and spec.source not in allowed:
            raise ValueError(
                f"bind mount source {spec.source!r} is not permitted from "
                f"pipeline step config (allowed: {sorted(allowed)}). Use the "
                "shared workspace volume for data, or 'needs: [docker]' for "
                "the docker socket."
            )
        validated.append(spec)
    return validated


def build_container_mounts(
    specs: Sequence[Union[MountSpec, dict]],
) -> tuple[dict, list[Mount]]:
    """Build docker-py mount kwargs from explicit MountSpecs.

    Returns (volumes_dict, mounts_list):
    - VOLUME specs land in the `volumes=` dict form (named volume -> bind spec);
      sources are validated against docker's volume-name grammar so a host
      path declared VOLUME raises instead of being silently bind-mounted.
    - BIND specs become typed docker.types.Mount(type='bind') objects so the
      engine never classifies the source by its shape (Windows C:\\ paths work).
    """
    volumes: dict = {}
    mounts: list[Mount] = []
    for raw in specs:
        spec = MountSpec.from_config(raw)
        if spec.addressing is MountAddressing.VOLUME:
            if not _VOLUME_NAME_RE.match(spec.source):
                raise ValueError(
                    f"{spec.source!r} is not a valid docker volume name; "
                    "declare addressing='bind' for host paths"
                )
            volumes[spec.source] = {"bind": spec.target, "mode": spec.mode}
        else:
            mounts.append(
                Mount(
                    target=spec.target,
                    source=spec.source,
                    type="bind",
                    read_only=(spec.mode == "ro"),
                )
            )
    return volumes, mounts


def ensure_network(docker_client: DockerClient, network_name: str) -> None:
    """Idempotently ensure the named docker network exists (sync - callers
    wrap in run_in_threadpool).

    Created with compose-compatible labels so a later `docker compose up`
    (which declares the same network name) adopts it instead of erroring.
    """
    try:
        docker_client.networks.get(network_name)
        return
    except NotFound:
        pass
    logger.info("Creating docker network %s", network_name)
    try:
        docker_client.networks.create(
            network_name,
            driver="bridge",
            labels={
                "com.docker.compose.network": network_name,
                "com.docker.compose.project": "lazyaf",
            },
        )
    except APIError:
        # Lost a creation race - fine as long as it exists now.
        docker_client.networks.get(network_name)


def build_step_command(step_config: dict, home_dir: str) -> Union[list, str]:
    """Build the container command for a step.

    - String commands are shell-wrapped as [shell, "-c", script] (default
      shell: bash) with a ``set -e`` prelude (12.2-INT fix 8): the step
      fails at the FIRST failing line instead of reporting only the last
      line's exit code - a multiline script whose middle command dies no
      longer reads as green. docker-py shlex-splits raw strings, which
      breaks multiline/piped scripts, hence the explicit shell. The wrapper
      also ensures $HOME exists on the shared workspace volume before the
      user script runs.
    - List commands (exec form) pass through untouched - explicit opt-out
      for images without a shell. List-form steps OWN their HOME: no shell
      prelude is possible, so they must create/populate $HOME themselves
      if they rely on it (documented contract, not inferred).
    - Images without bash can declare `shell: "sh"` in the step config
      (explicit, never inferred from the image).
    """
    command = step_config.get("command", "")
    if isinstance(command, (list, tuple)):
        return list(command)
    shell = step_config.get("shell", "bash")
    script = f"set -e\nmkdir -p {shlex.quote(home_dir)}\n{command}"
    return [shell, "-c", script]


def build_control_archive(files: Sequence[tuple[str, dict]]) -> bytes:
    """Build the in-memory tar delivering one or more `.control/<name>` files.

    Extracted by `container.put_archive("/workspace", ...)` onto the
    created-but-not-started step container, so secrets (the step JWT, the
    provider API key) never appear in `docker inspect` env and nothing is
    written to the backend's CWD. Every file is mode 0600. Tar entries carry
    no uid/gid: the image entrypoint's chown of /workspace/.control owns
    in-container readability (setting uid/gid 1000 here was redundant with
    it), and its `-name '*.json'` sweep already covers the agent payload.

    An agent step ships TWO entries in ONE tar (12.5): the step config and
    the agent config. One put_archive keeps them atomic with respect to
    container start.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        dir_info = tarfile.TarInfo(CONTROL_CONFIG_DIR)
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o700
        tar.addfile(dir_info)

        for filename, config in files:
            payload = json.dumps(config, indent=2).encode("utf-8")
            file_info = tarfile.TarInfo(f"{CONTROL_CONFIG_DIR}/{filename}")
            file_info.size = len(payload)
            file_info.mode = 0o600
            tar.addfile(file_info, io.BytesIO(payload))
    return buf.getvalue()


def build_step_config_archive(config: dict, filename: str) -> bytes:
    """Single-file convenience wrapper over `build_control_archive`."""
    return build_control_archive([(filename, config)])


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return "timeout" in text or "timed out" in text


class LocalExecutor:
    """
    Executes steps locally by spawning Docker containers.

    Example usage:
        executor = LocalExecutor(docker_client)
        async for event in executor.execute_step(config, context):
            if event["type"] == "log":
                print(event["line"])
            elif event["type"] == "result":
                print(f"Step {event['status']} with exit code {event['exit_code']}")
    """

    def __init__(self, docker_client: DockerClient):
        """
        Initialize executor with Docker client.

        Args:
            docker_client: Docker SDK client instance
        """
        self._docker = docker_client
        self._running_containers: dict[str, Any] = {}
        self._completed_executions: dict[str, dict] = {}  # For idempotency
        self._network_ready: set[str] = set()
        # Control-layer capability cache keyed by resolved IMAGE ID (never
        # by tag: a rebuilt tag gets a new ID and is re-evaluated fresh).
        self._control_label_cache: dict[str, bool] = {}
        # Generic label-declaration cache, keyed by (resolved image ID, label)
        # on the same "never by tag" rule as _control_label_cache above.
        self._declared_label_cache: dict[tuple[str, str], bool] = {}

    def reset(self) -> None:
        """Test-mode reset hook: clear the idempotency cache, the
        running-container registry (execution keys embed StepRun ids, which a
        DB reset is about to delete), and the image-label capability cache
        (images may be rebuilt between test runs). The docker client and
        network cache are environment-level and survive."""
        self._completed_executions.clear()
        self._running_containers.clear()
        self._control_label_cache.clear()
        self._declared_label_cache.clear()

    async def image_declares_label(self, image: str, label: str) -> bool | None:
        """Does `image` declare `label` with the value "1"? None = can't tell.

        The generic form of `image_supports_control_layer`'s rule, added so
        callers that need a DIFFERENT label - the agent-runtime preflight in
        `pipeline_executor` is the first - go through a public seam on this
        class instead of reaching into `executor._docker`. A stub or a future
        remote executor that does not implement this is then visibly missing
        a method rather than silently turning a preflight off.

        The three-valued return is the whole point: `False` means the image
        was inspected and does NOT declare the label, `None` means the
        inspection itself did not happen (unreachable daemon, missing tag).
        Collapsing `None` into `False` would report an infrastructure problem
        as "your image is wrong" and send the operator the wrong way, so
        callers must keep them apart.

        Cached by resolved image ID + label, so a rebuilt tag (new ID) is
        re-evaluated and repeat dispatches of the same build skip the inspect.
        """
        try:
            img = await run_in_threadpool(self._docker.images.get, image)
        except ImageNotFound:
            logger.warning(
                "Label inspection for %s=%s skipped: image %s is not present",
                label,
                LABEL_DECLARED_VALUE,
                image,
            )
            return None
        except Exception:
            logger.warning(
                "Label inspection for %s=%s failed on image %s",
                label,
                LABEL_DECLARED_VALUE,
                image,
                exc_info=True,
            )
            return None

        cache_key = (img.id, label)
        declared = self._declared_label_cache.get(cache_key)
        if declared is None:
            declared = (img.labels or {}).get(label) == LABEL_DECLARED_VALUE
            self._declared_label_cache[cache_key] = declared
        return declared

    async def image_supports_control_layer(self, image: str) -> bool:
        """Whether `image` bakes `lazyaf.control-layer=1` (label VALUE '1'
        required - presence alone is not a declaration).

        The mode decision input (12.3): label value "1" => control mode,
        anything else => stdout mode. The tag is resolved with images.get on
        EVERY dispatch (threadpooled, R5 - a local inspect is cheap) and the
        verdict is cached by the resolved image ID, so a rebuilt tag (new
        ID) is re-evaluated while repeat dispatches of the same build skip
        the label check. A missing image returns False - the run then fails
        loudly in execute_step's existing ImageNotFound handler. Other
        inspection errors also return False (stdout mode keeps stock-image
        behavior unchanged).
        """
        try:
            img = await run_in_threadpool(self._docker.images.get, image)
        except ImageNotFound:
            return False
        except Exception:
            logger.warning(
                "Label inspection failed for image %s; treating as stdout mode",
                image,
                exc_info=True,
            )
            return False
        image_id = img.id
        supports = self._control_label_cache.get(image_id)
        if supports is None:
            labels = img.labels or {}
            supports = labels.get(CONTROL_LAYER_LABEL) == "1"
            self._control_label_cache[image_id] = supports
        if supports:
            logger.info(
                "Dispatching control-labeled image %s (%s=1): control mode "
                "available",
                image,
                CONTROL_LAYER_LABEL,
            )
        elif image.startswith("lazyaf-"):
            logger.info(
                "Image %s is a lazyaf-* image WITHOUT %s=1; control mode NOT "
                "engaged - it runs in stdout mode",
                image,
                CONTROL_LAYER_LABEL,
            )
        return supports

    async def find_missing_images(self, images: Sequence[str]) -> list[str]:
        """Resolve step image tags at run start (12.3 image preflight).

        Returns the subset of `images` the daemon cannot resolve, so the
        pipeline executor can fail the run with ONE message naming every
        missing tag BEFORE dispatching step 0. Daemon hiccups are logged and
        treated as present - dispatch will surface them loudly per-step.
        """
        missing: list[str] = []
        for image in images:
            try:
                await run_in_threadpool(self._docker.images.get, image)
            except ImageNotFound:
                missing.append(image)
            except Exception:
                logger.warning(
                    "Image preflight inspection failed for %s; leaving it to "
                    "dispatch",
                    image,
                    exc_info=True,
                )
        return missing

    async def _ensure_network(self, network_name: str) -> None:
        """Ensure the step network exists (once per executor instance)."""
        if network_name in self._network_ready:
            return
        await run_in_threadpool(ensure_network, self._docker, network_name)
        self._network_ready.add(network_name)

    async def _cleanup_container(self, execution_key: str, container) -> None:
        """Remove a finished step container BEFORE the result event goes out
        (12.2-INT fix 6): step completion is synchronous with the container
        being gone, never dependent on generator GC. Docker calls run in the
        threadpool (R5); failures are logged, never raised."""
        self._running_containers.pop(execution_key, None)
        if container is None:
            return
        try:
            await run_in_threadpool(container.remove, force=True)
        except Exception:
            logger.warning(
                "Failed to remove step container for %s", execution_key, exc_info=True
            )

    def _schedule_orphan_container_removal(self, execution_key: str, container) -> None:
        """Backstop for ABNORMAL generator exits only (consumer closed the
        stream early / task cancelled): an async generator's finally cannot
        await during GeneratorExit, so removal is scheduled as its own task
        on the running loop (falling back to a blocking best-effort remove
        when no loop is available)."""

        async def _remove() -> None:
            try:
                await run_in_threadpool(container.remove, force=True)
            except Exception:
                logger.warning(
                    "Backstop removal of step container for %s failed",
                    execution_key,
                    exc_info=True,
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                container.remove(force=True)
            except Exception:
                pass
            return
        loop.create_task(_remove())

    async def execute_step(
        self,
        step_config: dict,
        execution_context: dict,
    ) -> AsyncGenerator[dict, None]:
        """
        Execute a step in a Docker container.

        Args:
            step_config: Step configuration including:
                - type: Step type (script, docker, agent)
                - command: Command to run (string -> shell-wrapped under
                  `set -e`; list -> exec form, owns its own HOME)
                - shell: Shell for wrapping string commands (default "bash")
                - image: Docker image (optional, default from settings.step_default_image)
                - working_dir: Working directory (optional, default from settings)
                - timeout: Timeout in seconds (optional)
                - environment: Additional environment variables (optional)
                - memory_limit: e.g. "512m" (optional)
                - mounts: extra mounts, each with EXPLICIT addressing:
                  {"addressing": "volume"|"bind", "source": ..., "target": ..., "mode": "rw"|"ro"}
                  Bind sources must be on the allowlist (default: the docker
                  socket only) - anything else fails the step loudly (fix 10).
                - secret_environment: env vars delivered ONLY through the
                  step config FILE (12.5). Never merged into the container's
                  `environment` kwarg, so they are absent from `docker
                  inspect`. Present WITHOUT control mode = the step fails at
                  dispatch ("secrets require control mode") rather than
                  downgrading a key onto the inspectable path.
                - agent: kwargs for `generate_agent_config` (12.5). When
                  present in control mode a SECOND file,
                  .control/agent.<step_execution_id>.json, ships in the same
                  put_archive tar and its path is announced through
                  LAZYAF_AGENT_CONFIG_PATH in the config FILE's environment.
                - usage_provider / role: non-secret usage-channel attribution
                  stamped into container env for run.py (M13 seam).

            execution_context: Execution context including:
                - pipeline_run_id: Pipeline run UUID
                - step_run_id: Step run UUID
                - step_index: Step index in pipeline
                - step_id: the step's GRAPH NODE ID (12.8). Absent/None on a
                  marker StepRun, which names no node.
                - execution_key: Unique key for idempotency
                - workspace_volume: Docker NAMED VOLUME for the workspace
                - control_mode: EXPLICIT reporting-mode flag, decided at
                  dispatch time (12.3) - never inferred here. When true,
                  step_execution_id and step_auth_token are REQUIRED: the
                  step config file is delivered to the created container via
                  put_archive and the in-container runtime reports through
                  POST /api/steps/*; container stdout stays observable but
                  is not the reporting path.
                - step_execution_id: StepExecution row id (control mode)
                - step_auth_token: per-step-execution JWT (control mode)

        Yields:
            Event dicts with "type" field:
                - {"type": "status", "status": "preparing"|"running"|etc}
                - {"type": "log", "line": "..."}
                - {"type": "result", "status": "completed"|"failed"|"timeout", "exit_code": int|None}

        By the time the "result" event is yielded, the step container has
        already been removed (fix 6).
        """
        execution_key = execution_context["execution_key"]

        # Check for cached result (idempotency)
        if execution_key in self._completed_executions:
            cached = self._completed_executions[execution_key]
            yield {"type": "status", "status": cached["status"]}
            yield {
                "type": "result",
                "status": cached["status"],
                "exit_code": cached.get("exit_code"),
                "cached": True,
            }
            return

        # Status: preparing
        yield {"type": "status", "status": "preparing"}

        settings = get_settings()
        control_mode = bool(execution_context.get("control_mode"))
        image = step_config.get("image", settings.step_default_image)
        timeout = step_config.get("timeout", 300)  # Default 5 minutes
        user_env = step_config.get("environment", {})
        # Secrets (12.5): provider API keys travel ONLY in the step config
        # FILE - the one channel the container runtime reads that never
        # reaches `docker inspect`. NEVER merged into run_kwargs/create_kwargs
        # "environment"; the split is enforced below and asserted by a real-
        # docker T2 test that greps the created container's inspect output.
        secret_env = step_config.get("secret_environment") or {}
        agent_payload = step_config.get("agent")
        memory_limit = step_config.get("memory_limit")  # e.g., "512m", "1g"
        working_dir = step_config.get("working_dir", settings.step_working_dir)
        # Control mode: the container runs the image's control entrypoint
        # (command=None; the entrypoint ignores CMD when LAZYAF_CONTROL=1)
        # and the RAW command string travels in the config file - the
        # runtime shell-wraps it with the same `set -e` semantics as
        # build_step_command, so scripts behave identically in both modes.
        command = None if control_mode else build_step_command(
            step_config, settings.step_home_dir
        )

        # Build environment variables. HOME lives on the shared workspace
        # volume so tools installed in one step persist to the next; an
        # explicit HOME in the step's environment wins. LAZYAF_BACKEND_URL
        # (12.2-INT fix 12) lets in-container tooling reach the backend on
        # the shared network; settings.container_backend_url is owned by a
        # parallel change to config.py - read defensively.
        environment = {
            "HOME": settings.step_home_dir,
            **user_env,
            "LAZYAF_PIPELINE_RUN_ID": execution_context["pipeline_run_id"],
            "LAZYAF_STEP_RUN_ID": execution_context["step_run_id"],
            "LAZYAF_STEP_INDEX": str(execution_context["step_index"]),
            "LAZYAF_EXECUTION_KEY": execution_key,
            "LAZYAF_BACKEND_URL": getattr(
                settings, "container_backend_url", "http://backend:8000"
            ),
            # The entrypoint's mode switch (images/base entrypoint contract):
            # 1 => exec the control runtime; 0 => CMD passthrough (the image
            # degrades to a stock image). Non-secret; the auth token travels
            # ONLY in the config file, never in inspectable env.
            "LAZYAF_CONTROL": "1" if control_mode else "0",
        }

        # Usage-channel attribution (12.5 / M13 seam). All NON-SECRET, so
        # they travel in ordinary container env: run.py reads them when it
        # composes the usage manifest, and a step that never produces one
        # still gets a fallback record stamped with the right provider.
        # LAZYAF_ROLE / LAZYAF_GPU_* are empty in 12.5 - nothing sets them
        # until strategies (M13) and self-hosted nodes (12.6) exist.
        environment["LAZYAF_USAGE_PROVIDER"] = str(
            step_config.get("usage_provider") or DEFAULT_USAGE_PROVIDER
        )
        # LAZYAF_STEP_ID (12.8): the step's GRAPH NODE ID - the id its author
        # wrote in the pipeline definition, and the key `StepRun.step_id`
        # carries. It travels alongside LAZYAF_STEP_INDEX rather than
        # replacing it: the index is still how the websocket frames, the
        # execution key and the state machine address a step, but it is
        # DERIVED from `list(steps_dict.keys()).index(step_id)`, so anything
        # in-container that needs to know WHICH STEP IT IS should ask by id.
        # scripts/verify_executor.py's self-exemption is the first such
        # reader. Emitted only when the StepRun names a node: a marker row
        # (`_trigger_card`) deliberately carries step_id=None, and an absent
        # variable says "this run step is not a graph node" honestly, where
        # an empty string would read as a node whose id is "".
        for key, source in (
            ("LAZYAF_STEP_ID", execution_context.get("step_id")),
            ("LAZYAF_ROLE", step_config.get("role")),
            ("LAZYAF_GPU_NODE_ID", execution_context.get("gpu_node_id")),
            ("LAZYAF_GPU_FRACTION", execution_context.get("gpu_fraction")),
        ):
            if source not in (None, ""):
                environment[key] = str(source)


        # Build mounts via explicit addressing (never inferred from path
        # shape). The workspace volume is internal; step-config mounts go
        # through the bind allowlist gate (fix 10).
        workspace_volume = execution_context["workspace_volume"]
        container = None
        final_result: dict | None = None
        # Control mode ships NO per-line log events (the router is the sole
        # log writer, R3 - shipping them too was pure double work); the pump
        # keeps a bounded in-memory tail instead, surfaced on the result
        # event for failure/zero-log forensics (12.3).
        log_tail: deque[str] | None = (
            deque(maxlen=CONTROL_MODE_LOG_TAIL_LINES) if control_mode else None
        )
        try:
            if secret_env and not control_mode:
                # A secret must never be able to silently DOWNGRADE onto the
                # stdout path, where the only delivery channel left is
                # container env - i.e. `docker inspect`. Fail at dispatch and
                # name the reason (the ValueError handler below turns this
                # into a loud "step config error" result event).
                raise ValueError(
                    "secrets require control mode: this step declares "
                    f"secret_environment ({', '.join(sorted(secret_env))}) "
                    "but is dispatched in stdout mode, where the only "
                    "delivery channel is inspectable container environment. "
                    "Use a control-layer image and do not set `control: false`."
                )

            mount_specs: list = [
                MountSpec(
                    addressing=MountAddressing.VOLUME,
                    source=workspace_volume,
                    target="/workspace",
                    mode="rw",
                )
            ]
            mount_specs.extend(
                validate_step_mounts(
                    step_config.get("mounts", []), bind_mount_allowlist()
                )
            )
            volumes, extra_mounts = build_container_mounts(mount_specs)

            # Build container run kwargs
            run_kwargs = {
                "command": command,
                "detach": True,
                "volumes": volumes,
                "working_dir": working_dir,
                "environment": environment,
                "network": settings.container_network,
                "remove": False,  # We'll remove it ourselves after getting logs
                "labels": {
                    CONTAINER_LABEL_EXECUTION_KEY: execution_key,
                    CONTAINER_LABEL_PIPELINE_RUN: execution_context["pipeline_run_id"],
                },
            }
            if extra_mounts:
                run_kwargs["mounts"] = extra_mounts

            # Add memory limit if specified
            if memory_limit:
                run_kwargs["mem_limit"] = memory_limit

            await self._ensure_network(settings.container_network)

            if control_mode:
                # Control mode (12.3): create -> put_archive the step config
                # onto the workspace volume -> start. Mounts are bound at
                # create, so the volume is addressable before the runtime
                # boots; the token never appears in `docker inspect` env.
                # Preconditions (string command, execution id + token) are
                # the DISPATCHER's job (_prepare_control_mode is the single
                # owner) - the executor trusts its dispatch; a broken
                # context fails loudly via the generic error handler.
                step_execution_id = execution_context["step_execution_id"]
                raw_command = step_config.get("command", "")
                # Producer stays the single source of the file shape (R3):
                # verbatim generate_step_config output.
                from app.services.control_layer.workspace import (
                    generate_agent_config,
                    generate_step_config,
                )

                # The FILE environment is the secret channel (12.5): the
                # in-container executor does env.update(config.environment)
                # before Popen, so these reach the step process without ever
                # entering the container's inspectable env.
                agent_filename = f"{AGENT_CONFIG_PREFIX}{step_execution_id}.json"
                file_environment = {**user_env, **secret_env}
                if agent_payload is not None:
                    file_environment[AGENT_CONFIG_PATH_ENV] = (
                        f"/workspace/{CONTROL_CONFIG_DIR}/{agent_filename}"
                    )

                config_kwargs = dict(
                    step_id=step_execution_id,
                    step_run_id=execution_context["step_run_id"],
                    execution_key=execution_key,
                    command=raw_command,
                    backend_url=getattr(
                        settings, "container_backend_url", "http://backend:8000"
                    ),
                    auth_token=execution_context["step_auth_token"],
                    environment=file_environment,
                    timeout_seconds=timeout,
                    working_directory=working_dir,
                )
                try:
                    # Cross-agent contract #2: the config carries a "shell"
                    # key sourced from step config (default "bash"); the
                    # producer (generate_step_config) owns the file shape.
                    config_payload = generate_step_config(
                        **config_kwargs, shell=step_config.get("shell", "bash")
                    )
                except TypeError:
                    logger.error(
                        "generate_step_config does not accept 'shell' yet "
                        "(cross-agent contract #2 not landed); dispatching "
                        "the step config without a shell key"
                    )
                    config_payload = generate_step_config(**config_kwargs)

                # Per-step config path (cross-agent contract #1): one file
                # per step execution on the SHARED workspace volume, and
                # CONFIG_PATH pointing the runtime at exactly that file -
                # parallel steps of one run can no longer clobber each
                # other's config.
                config_filename = f"{step_execution_id}.json"
                create_kwargs = {
                    k: v
                    for k, v in run_kwargs.items()
                    if k not in ("detach", "remove")
                }
                create_kwargs["environment"] = {
                    **environment,
                    "CONFIG_PATH": (
                        f"/workspace/{CONTROL_CONFIG_DIR}/{config_filename}"
                    ),
                }
                # Agent steps (12.5): the SECOND file, produced by the single
                # agent-config producer and shipped in the SAME tar. Built
                # BEFORE `create` so an invalid agent payload fails the step
                # without leaving a container behind.
                archive_files: list[tuple[str, dict]] = [
                    (config_filename, config_payload)
                ]
                if agent_payload is not None:
                    archive_files.append(
                        (agent_filename, generate_agent_config(**agent_payload))
                    )

                container = await run_in_threadpool(
                    self._docker.containers.create, image, **create_kwargs
                )
                self._running_containers[execution_key] = container
                await run_in_threadpool(
                    container.put_archive,
                    "/workspace",
                    build_control_archive(archive_files),
                )
                await run_in_threadpool(container.start)
            else:
                # Spawn container (blocking SDK call off the event loop, R5)
                container = await run_in_threadpool(
                    self._docker.containers.run, image, **run_kwargs
                )
                self._running_containers[execution_key] = container

            # Status: running
            yield {"type": "status", "status": "running"}

            # Stream logs from a pump thread through an asyncio queue so the
            # event loop never blocks and the deadline can fire mid-stream.
            # In control mode the runtime enforces timeout_seconds
            # in-container (graceful); this deadline moves out by a bounded
            # grace and remains purely the backstop.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout + (
                CONTROL_MODE_TIMEOUT_GRACE if control_mode else 0
            )
            queue: asyncio.Queue = asyncio.Queue()

            def _put(item) -> None:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
                except RuntimeError:
                    pass  # Event loop closed - consumer is gone

            def _pump() -> None:
                try:
                    for raw_line in container.logs(stream=True, follow=True):
                        if log_tail is not None:
                            line = raw_line
                            if isinstance(line, bytes):
                                line = line.decode("utf-8", errors="replace")
                            log_tail.append(line.rstrip("\n"))
                        else:
                            _put(("log", raw_line))
                except Exception as exc:  # Surface stream errors to the consumer
                    _put(("error", exc))
                finally:
                    _put(("eof", None))

            pump_thread = threading.Thread(
                target=_pump, name=f"lazyaf-logs-{execution_key}", daemon=True
            )
            pump_thread.start()

            timed_out = False
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                if kind == "eof":
                    break
                if kind == "error":
                    raise payload
                log_line = payload
                if isinstance(log_line, bytes):
                    log_line = log_line.decode("utf-8", errors="replace")
                yield {"type": "log", "line": log_line.rstrip("\n")}

            if timed_out:
                try:
                    await run_in_threadpool(container.kill)
                except Exception:
                    pass
                final_result = {
                    "type": "result",
                    "status": "timeout",
                    "exit_code": None,
                    "timeout_seconds": timeout,
                }
            else:
                # Wait for the container to finish within what's left of the
                # deadline.
                remaining = max(deadline - loop.time(), 0.001)
                try:
                    result = await run_in_threadpool(container.wait, timeout=remaining)
                    exit_code = result.get("StatusCode", -1)
                    final_result = {
                        "type": "result",
                        "status": "completed" if exit_code == 0 else "failed",
                        "exit_code": exit_code,
                    }
                except Exception as e:
                    if not _is_timeout_error(e):
                        raise
                    # Timed out waiting: kill the container
                    try:
                        await run_in_threadpool(container.kill)
                    except Exception:
                        pass
                    final_result = {
                        "type": "result",
                        "status": "timeout",
                        "exit_code": None,
                        "timeout_seconds": timeout,
                    }

        except (GeneratorExit, asyncio.CancelledError):
            # Abnormal exit: the consumer closed the stream early or the
            # consuming task was cancelled. Cleanup must not depend on GC
            # (fix 6) and cannot be awaited here - schedule it.
            self._running_containers.pop(execution_key, None)
            if container is not None:
                self._schedule_orphan_container_removal(execution_key, container)
                container = None
            raise

        except ImageNotFound:
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Image not found: {image}",
            }

        except ContainerError as e:
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": e.exit_status,
                "error": str(e),
            }

        except APIError as e:
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Docker API error: {str(e)}",
            }

        except DockerException as e:
            # Catch-all for Docker connection issues (connection refused, etc.)
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Docker unavailable: {str(e)}",
            }

        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
            # Handle request timeouts to Docker daemon
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Docker connection timeout: {str(e)}",
            }

        except ValueError as e:
            # Step configuration errors (mount policy/addressing, ...) fail
            # the step loudly with the config error verbatim (fix 10).
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"step config error: {e}",
            }

        except Exception as e:
            # Catch-all for unexpected errors
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Unexpected error: {str(e)}",
            }

        # Terminal tail - ONE exit path (fix 6): remove the container and the
        # registry entry FIRST (threadpooled), then emit the terminal status
        # and the result. A consumer that stops at the result event therefore
        # observes the container already gone; nothing waits on generator GC.
        await self._cleanup_container(execution_key, container)
        if log_tail:
            # Forensics tail (12.3): the finish path persists it when the
            # step failed or the router landed zero log bytes.
            final_result["log_tail"] = list(log_tail)
        yield {"type": "status", "status": final_result["status"]}
        self._completed_executions[execution_key] = final_result
        yield final_result

    async def cancel_step(self, execution_key: str) -> bool:
        """
        Cancel a running step by killing its container.

        Args:
            execution_key: The execution key of the step to cancel

        Returns:
            True if container was found and killed, False otherwise
        """
        container = self._running_containers.get(execution_key)
        if not container:
            return False

        try:
            await run_in_threadpool(container.kill)
            return True
        except Exception:
            return False

    async def cancel_all(self) -> int:
        """Kill every tracked running container (safe-teardown hook for
        test-mode reset / shutdown: killing the containers ends their event
        streams naturally so consumers finish without being hard-cancelled
        mid-commit). Returns the number killed; never raises."""
        killed = 0
        for execution_key, container in list(self._running_containers.items()):
            try:
                await run_in_threadpool(container.kill)
                killed += 1
            except Exception:
                logger.warning(
                    "cancel_all: failed to kill container for %s",
                    execution_key,
                    exc_info=True,
                )
        return killed
