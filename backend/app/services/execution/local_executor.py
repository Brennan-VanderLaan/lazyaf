"""
Local Executor - Docker-based step execution.

Spawns Docker containers directly from the backend via Docker SDK.
Provides:
- Container spawning with proper configuration
- Workspace volume mounting at /workspace
- Real-time log streaming
- Timeout handling (kills container after deadline)
- Crash detection
- Idempotent execution (same key = cached result)
"""
import asyncio
from datetime import datetime
from typing import AsyncGenerator, Any
from docker import DockerClient
from docker.errors import ContainerError, ImageNotFound, APIError, DockerException
import requests.exceptions


# Default image for steps that don't specify one
DEFAULT_STEP_IMAGE = "python:3.12-slim"


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
                - command: Command to run
                - image: Docker image (optional, uses default if not specified)
                - timeout: Timeout in seconds (optional)
                - environment: Additional environment variables (optional)

            execution_context: Execution context including:
                - pipeline_run_id: Pipeline run UUID
                - step_run_id: Step run UUID
                - step_index: Step index in pipeline
                - execution_key: Unique key for idempotency
                - workspace_volume: Docker volume name for workspace
                - repo_url: Git repository URL
                - branch: Branch to checkout

        Yields:
            Event dicts with "type" field:
                - {"type": "status", "status": "preparing"|"running"|etc}
                - {"type": "log", "line": "..."}
                - {"type": "result", "status": "completed"|"failed", "exit_code": int}
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

        image = step_config.get("image", DEFAULT_STEP_IMAGE)
        command = step_config.get("command", "")
        timeout = step_config.get("timeout", 300)  # Default 5 minutes
        user_env = step_config.get("environment", {})
        memory_limit = step_config.get("memory_limit")  # e.g., "512m", "1g"

        # Build environment variables
        environment = {
            **user_env,
            "LAZYAF_PIPELINE_RUN_ID": execution_context["pipeline_run_id"],
            "LAZYAF_STEP_RUN_ID": execution_context["step_run_id"],
            "LAZYAF_STEP_INDEX": str(execution_context["step_index"]),
            "LAZYAF_EXECUTION_KEY": execution_key,
        }

        # Build volume mounts
        workspace_volume = execution_context["workspace_volume"]
        volumes = {
            workspace_volume: {"bind": "/workspace", "mode": "rw"},
        }

        container = None
        try:
            # Build container run kwargs
            run_kwargs = {
                "command": command,
                "detach": True,
                "volumes": volumes,
                "working_dir": "/workspace/repo",
                "environment": environment,
                "remove": False,  # We'll remove it ourselves after getting logs
            }

            # Add memory limit if specified
            if memory_limit:
                run_kwargs["mem_limit"] = memory_limit

            # Spawn container
            container = self._docker.containers.run(image, **run_kwargs)

            self._running_containers[execution_key] = container

            # Status: running
            yield {"type": "status", "status": "running"}

            # Stream logs
            for log_line in container.logs(stream=True, follow=True):
                if isinstance(log_line, bytes):
                    log_line = log_line.decode("utf-8", errors="replace")
                yield {"type": "log", "line": log_line.rstrip("\n")}

            # Wait for container to finish with timeout
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", -1)
            except Exception as e:
                # Timeout or other error
                if "timeout" in str(e).lower() or isinstance(e, TimeoutError):
                    # Kill the container
                    try:
                        container.kill()
                    except Exception:
                        pass

                    yield {"type": "status", "status": "timeout"}
                    final_result = {
                        "type": "result",
                        "status": "timeout",
                        "exit_code": None,
                        "timeout_seconds": timeout,
                    }
                    self._completed_executions[execution_key] = final_result
                    yield final_result
                    return
                raise

            # Determine final status based on exit code
            if exit_code == 0:
                status = "completed"
            else:
                status = "failed"

            yield {"type": "status", "status": status}

            final_result = {
                "type": "result",
                "status": status,
                "exit_code": exit_code,
            }
            self._completed_executions[execution_key] = final_result
            yield final_result

        except ImageNotFound as e:
            yield {"type": "status", "status": "failed"}
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Image not found: {image}",
            }
            self._completed_executions[execution_key] = final_result
            yield final_result

        except ContainerError as e:
            yield {"type": "status", "status": "failed"}
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": e.exit_status,
                "error": str(e),
            }
            self._completed_executions[execution_key] = final_result
            yield final_result

        except APIError as e:
            yield {"type": "status", "status": "failed"}
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Docker API error: {str(e)}",
            }
            self._completed_executions[execution_key] = final_result
            yield final_result

        except DockerException as e:
            # Catch-all for Docker connection issues (connection refused, etc.)
            yield {"type": "status", "status": "failed"}
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Docker unavailable: {str(e)}",
            }
            self._completed_executions[execution_key] = final_result
            yield final_result

        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
            # Handle request timeouts to Docker daemon
            yield {"type": "status", "status": "failed"}
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Docker connection timeout: {str(e)}",
            }
            self._completed_executions[execution_key] = final_result
            yield final_result

        except Exception as e:
            # Catch-all for unexpected errors
            yield {"type": "status", "status": "failed"}
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": f"Unexpected error: {str(e)}",
            }
            self._completed_executions[execution_key] = final_result
            yield final_result

        finally:
            # Cleanup: remove from running containers
            if execution_key in self._running_containers:
                del self._running_containers[execution_key]

            # Cleanup: remove container
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass  # Best effort cleanup

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
            container.kill()
            return True
        except Exception:
            return False
