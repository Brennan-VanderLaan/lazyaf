"""
Workspace population - Phase 12.2-INT.

Populates a workspace's named docker volume by spawning a short-lived helper
container on the shared container network that clones the repo from the
internal git server into /workspace/repo (LocalExecutor's working_dir).

Contract:
    await populate_workspace(volume_name, repo_id, branch, commit_sha)

- Image: settings.workspace_clone_image (default python:3.12 - has bash+git).
- Network: settings.container_network (backend's git URL resolves there).
- Clone URL: settings.container_git_url_template.format(repo_id=repo_id).
  TEST SEAM: integration tests monkeypatch the template on the cached
  settings instance to a file:// URL served from a bind mount (passed via
  `extra_mounts`), so population is exercised against a real named volume
  without requiring a running backend; one e2e-lane test covers the real
  http URL when E2E_BACKEND_URL/BACKEND_URL is set.
- All docker SDK calls run off the event loop (run_in_threadpool, R5).
- The helper container is always removed (finally - equivalent to
  auto-remove, but deferred so failure log tails can be captured).
- Failure raises WorkspacePopulationError including the container log tail.
"""
import logging
import shlex
from typing import Sequence, Union

import docker
from docker.errors import DockerException
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.services.execution.local_executor import (
    MountAddressing,
    MountSpec,
    build_container_mounts,
    ensure_network,
)

logger = logging.getLogger(__name__)

# Hard ceiling on how long a clone may take before the helper is killed.
DEFAULT_POPULATION_TIMEOUT_SECONDS = 300

# How much of the helper container's output ends up in the error message.
LOG_TAIL_LINES = 50


class WorkspacePopulationError(RuntimeError):
    """Raised when the workspace clone helper container fails."""


def default_docker_client():
    """Build the default docker client (cross-file contract #1).

    Default clients come from local_executor.make_docker_client, which
    honors settings.docker_host. Imported at CALL time so this module
    tolerates make_docker_client landing in a parallel change; the
    fallback constructs an equivalent client and warns loudly so the
    missing seam cannot go unnoticed.
    """
    try:
        from app.services.execution.local_executor import make_docker_client
    except ImportError:
        logger.warning(
            "local_executor.make_docker_client is not available yet; building "
            "a docker client directly (settings.docker_host honored). This is "
            "a transitional fallback for contract #1 — it should disappear "
            "once make_docker_client lands."
        )
        docker_host = get_settings().docker_host
        if docker_host:
            return docker.DockerClient(base_url=docker_host)
        return docker.from_env()
    return make_docker_client()


def _log_if_image_missing(client, image: str, purpose: str) -> None:
    """Explain the silence before docker-py's implicit first-run pull."""
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        logger.info(
            "pulling image %s for %s (not present locally; first run may take "
            "a while)...",
            image,
            purpose,
        )
    except Exception:  # daemon hiccup: the run call will surface it loudly
        pass


def _pre_pull_sync(client=None) -> None:
    """Blocking body of pre_pull_images — runs in the threadpool."""
    settings = get_settings()
    if client is None:
        client = default_docker_client()
    for image in dict.fromkeys([settings.step_default_image, settings.workspace_clone_image]):
        try:
            client.images.get(image)
            logger.debug("Image %s already present; skipping pre-pull", image)
            continue
        except docker.errors.ImageNotFound:
            pass
        except Exception:
            logger.warning(
                "Image pre-pull: could not inspect %s (docker unreachable?); "
                "it will be pulled implicitly on first use",
                image,
                exc_info=True,
            )
            continue
        logger.info("pulling image %s (startup pre-pull; may take a while)...", image)
        try:
            client.images.pull(image)
            logger.info("Pre-pulled image %s", image)
        except Exception:
            logger.warning(
                "Startup pre-pull of image %s failed; it will be pulled "
                "implicitly on first use",
                image,
                exc_info=True,
            )


async def pre_pull_images(client=None) -> None:
    """Pre-pull the step/clone images at startup (wired as a non-blocking
    background task in main.py's lifespan) so the first pipeline run does
    not sit silently through a multi-minute implicit pull. Never raises —
    failures are logged and the implicit pull remains the safety net."""
    try:
        await run_in_threadpool(_pre_pull_sync, client)
    except Exception:
        logger.exception("Image pre-pull task failed")


def _build_clone_script(clone_url: str, branch: str, commit_sha: str | None) -> str:
    """Build the bash script the helper container runs."""
    lines = [
        "set -e",
        f"git clone --branch {shlex.quote(branch)} -- {shlex.quote(clone_url)} /workspace/repo",
        "cd /workspace/repo",
    ]
    if commit_sha:
        lines.append(f"git checkout --detach {shlex.quote(commit_sha)}")
    return "\n".join(lines)


def _log_tail(container) -> str:
    """Best-effort tail of the helper container's combined output."""
    try:
        raw = container.logs(stdout=True, stderr=True, tail=LOG_TAIL_LINES)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return "<log tail unavailable>"


def _populate_sync(
    volume_name: str,
    repo_id: str,
    branch: str,
    commit_sha: str | None,
    client,
    extra_mounts: Sequence[Union[MountSpec, dict]],
    timeout: float,
) -> None:
    """Blocking population body - runs in the threadpool."""
    settings = get_settings()
    clone_url = settings.container_git_url_template.format(repo_id=repo_id)
    script = _build_clone_script(clone_url, branch, commit_sha)

    if client is None:
        client = default_docker_client()
    ensure_network(client, settings.container_network)

    specs: list = [
        MountSpec(
            addressing=MountAddressing.VOLUME,
            source=volume_name,
            target="/workspace",
            mode="rw",
        )
    ]
    specs.extend(extra_mounts)
    volumes, mounts = build_container_mounts(specs)

    run_kwargs: dict = {
        "command": ["bash", "-c", script],
        "detach": True,
        "volumes": volumes,
        "network": settings.container_network,
        "remove": False,  # removed in finally so a failure's log tail survives
        "labels": {"lazyaf.role": "workspace-populate", "lazyaf.volume": volume_name},
    }
    if mounts:
        run_kwargs["mounts"] = mounts

    logger.info(
        "Populating workspace volume %s (repo_id=%s branch=%s commit=%s) via %s",
        volume_name, repo_id, branch, commit_sha or "<branch head>", clone_url,
    )

    _log_if_image_missing(client, settings.workspace_clone_image, "workspace population")
    container = client.containers.run(settings.workspace_clone_image, **run_kwargs)
    try:
        try:
            result = container.wait(timeout=timeout)
        except Exception as exc:  # bounded wait expired or daemon hiccup
            try:
                container.kill()
            except Exception:
                pass
            raise WorkspacePopulationError(
                f"Workspace population for volume {volume_name!r} "
                f"(repo_id={repo_id!r}, branch={branch!r}) did not finish within "
                f"{timeout}s: {exc}\n--- helper log tail ---\n{_log_tail(container)}"
            ) from exc

        exit_code = result.get("StatusCode", -1)
        if exit_code != 0:
            raise WorkspacePopulationError(
                f"Workspace population for volume {volume_name!r} "
                f"(repo_id={repo_id!r}, branch={branch!r}, commit={commit_sha!r}) "
                f"failed with exit code {exit_code}\n"
                f"--- helper log tail ---\n{_log_tail(container)}"
            )
        logger.info("Workspace volume %s populated", volume_name)
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass  # Best effort cleanup


async def populate_workspace(
    volume_name: str,
    repo_id: str,
    branch: str,
    commit_sha: str | None,
    *,
    client=None,
    extra_mounts: Sequence[Union[MountSpec, dict]] = (),
    timeout: float = DEFAULT_POPULATION_TIMEOUT_SECONDS,
) -> None:
    """Populate a workspace named volume with a clone of the repo.

    Spawns a short-lived helper container (settings.workspace_clone_image) on
    settings.container_network that clones
    settings.container_git_url_template.format(repo_id=repo_id) into
    /workspace/repo on the named volume, checking out commit_sha when given.

    Args:
        volume_name: Docker NAMED volume to populate (mounted at /workspace).
        repo_id: Repo id substituted into the clone URL template.
        branch: Branch to clone.
        commit_sha: Optional commit to check out (detached) after clone.
        client: Injected docker client (WorkspaceService passes its own so
            the whole lifecycle rides one seam); None builds the default
            via default_docker_client() inside the threadpool.
        extra_mounts: Additional MountSpecs for the helper container
            (test seam - e.g. a read-only bind of a file:// seed repo).
        timeout: Bounded wait for the clone, seconds.

    Raises:
        WorkspacePopulationError: clone failed or timed out (message includes
            the helper container's log tail).
        docker.errors.DockerException: docker daemon unreachable.
    """
    try:
        await run_in_threadpool(
            _populate_sync, volume_name, repo_id, branch, commit_sha,
            client, extra_mounts, timeout,
        )
    except (WorkspacePopulationError, DockerException):
        raise
    except Exception as exc:
        # Anything unexpected still surfaces as a population failure, loudly.
        raise WorkspacePopulationError(
            f"Workspace population for volume {volume_name!r} failed: {exc}"
        ) from exc
