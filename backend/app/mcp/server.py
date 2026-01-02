"""
LazyAF MCP Server implementation.

This module defines the MCP server with tools for managing repos, cards, and jobs.
The server communicates with the FastAPI backend via HTTP to avoid DB conflicts.

For Claude Desktop: runs with stdio transport via `python -m app.mcp`
For other clients: SSE endpoint mounted at /mcp on FastAPI app
"""
import os

import httpx
from mcp.server.fastmcp import FastMCP

# Backend URL - defaults to localhost:8000, configurable via env
BACKEND_URL = os.environ.get("LAZYAF_BACKEND_URL", "http://localhost:8000")

mcp = FastMCP(
    name="lazyaf",
    instructions="""LazyAF is a visual orchestrator for AI agents and CI/CD tasks.

Use these tools to:
- List and inspect repositories
- Create and manage cards (feature requests or CI tasks)
- Create and run pipelines (multi-step workflows)
- Start work on cards (AI agent or script/docker execution)
- Monitor job progress and logs
- Check runner availability
- Manage agent files (reusable prompt templates)
- View branches and diffs

Card Types (step_type):
- "agent": AI implements a feature using Claude Code or Gemini CLI
- "script": Run a shell command directly in the cloned repo
- "docker": Run a command inside a Docker container

Card Lifecycle:
- todo -> in_progress (when started) -> in_review (when job completes)
- in_review: approve_card() to merge, reject_card() to reset to todo
- failed: retry_card() to try again, reject_card() to reset

Pipeline Features:
- Chain multiple steps (script, docker, agent) into reusable workflows
- Conditional branching: on_success and on_failure actions
- Actions: "next" (continue), "stop" (end), "trigger:{card_id}" (spawn AI fix), "merge:{branch}"
- Automatic triggers: card_complete (when card reaches status), push (on git push)
- Trigger actions: on_pass (merge card), on_fail (mark failed or reject)

Agent Files:
- Platform agent files: reusable prompt templates stored in LazyAF
- Repo agent files: defined in .lazyaf/agents/ directory (use list_repo_agents)
- Use {{title}} and {{description}} placeholders in templates

Typical card workflow:
1. list_repos() to see available repositories
2. list_cards(repo_id) to see existing work items
3. get_runner_status() to check available runners
4. create_card(repo_id, title, description, ...) to add new work
   - For AI work: step_type="agent", runner_type="any"|"claude-code"|"gemini"
   - For CI tasks: step_type="script", command="npm test"
   - For containerized CI: step_type="docker", image="node:20", command="npm test"
5. start_card(card_id) to trigger execution
6. get_job_logs(job_id) to monitor progress
7. When in_review: approve_card(card_id) to merge or reject_card(card_id) to reset

Pipeline workflow:
1. create_pipeline(repo_id, name, steps, triggers) to define a multi-step workflow
   - Add triggers to auto-run on card completion or git push
2. run_pipeline(pipeline_id) to start execution manually
3. get_pipeline_run(run_id) to monitor progress
4. Pipelines with triggers run automatically when conditions are met
"""
)


def _get_client() -> httpx.Client:
    """Get an HTTP client for backend communication."""
    return httpx.Client(base_url=BACKEND_URL, timeout=30.0)


@mcp.tool()
def list_repos() -> dict:
    """
    List all repositories in LazyAF.

    Returns repos in {"repos": [...]} format with ID, name, ingest status, and default branch.
    Only ingested repos can have cards started on them.
    """
    with _get_client() as client:
        response = client.get("/api/repos")
        if response.status_code != 200:
            return {"error": f"Failed to list repos: {response.text}"}
        return {"repos": response.json()}


@mcp.tool()
def list_cards(repo_id: str) -> dict:
    """
    List all cards for a repository.

    Args:
        repo_id: The repository ID (UUID string)

    Returns cards in {"cards": [...]} format with status, branch, and job info.
    """
    with _get_client() as client:
        response = client.get(f"/api/repos/{repo_id}/cards")
        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to list cards: {response.text}"}
        return {"cards": response.json()}


@mcp.tool()
def create_card(
    repo_id: str,
    title: str,
    description: str = "",
    runner_type: str = "any",
    step_type: str = "agent",
    command: str = "",
    image: str = "",
    working_dir: str = ""
) -> dict:
    """
    Create a new card in a repository.

    Args:
        repo_id: The repository ID (UUID string)
        title: Card title describing the feature or task
        description: Detailed description of what needs to be done
        runner_type: Which runner type to use - "any" (default), "claude-code", or "gemini"
            Only relevant for step_type="agent"
        step_type: Type of step - "agent" (AI implements feature), "script" (run shell command),
            or "docker" (run command in container). Default is "agent".
        command: Shell command to run. Required for step_type="script" or "docker".
        image: Docker image to use. Required for step_type="docker".
        working_dir: Working directory for script steps (relative to repo root).

    Returns the created card details.
    """
    # Validate runner_type
    valid_runner_types = ["any", "claude-code", "gemini"]
    if runner_type not in valid_runner_types:
        return {"error": f"Invalid runner_type '{runner_type}'. Must be one of: {', '.join(valid_runner_types)}"}

    # Validate step_type
    valid_step_types = ["agent", "script", "docker"]
    if step_type not in valid_step_types:
        return {"error": f"Invalid step_type '{step_type}'. Must be one of: {', '.join(valid_step_types)}"}

    # Validate required fields for non-agent steps
    if step_type == "script" and not command:
        return {"error": "command is required for step_type='script'"}
    if step_type == "docker":
        if not command:
            return {"error": "command is required for step_type='docker'"}
        if not image:
            return {"error": "image is required for step_type='docker'"}

    # Build step_config
    step_config = None
    if step_type != "agent":
        step_config = {}
        if command:
            step_config["command"] = command
        if image and step_type == "docker":
            step_config["image"] = image
        if working_dir and step_type == "script":
            step_config["working_dir"] = working_dir

    with _get_client() as client:
        payload = {
            "title": title,
            "description": description,
            "runner_type": runner_type,
            "step_type": step_type,
        }
        if step_config:
            payload["step_config"] = step_config

        response = client.post(f"/api/repos/{repo_id}/cards", json=payload)
        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found"}
        if response.status_code not in (200, 201):
            return {"error": f"Failed to create card: {response.text}"}

        card = response.json()
        card["message"] = "Card created successfully"
        return card


@mcp.tool()
def get_card(card_id: str) -> dict:
    """
    Get full details of a card including job information.

    Args:
        card_id: The card ID (UUID string)

    Returns complete card details with associated job info and logs preview.
    """
    with _get_client() as client:
        response = client.get(f"/api/cards/{card_id}")
        if response.status_code == 404:
            return {"error": f"Card {card_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to get card: {response.text}"}

        card = response.json()

        # If card has a job, fetch job details
        if card.get("job_id"):
            job_response = client.get(f"/api/jobs/{card['job_id']}")
            if job_response.status_code == 200:
                job = job_response.json()
                logs_preview = ""
                if job.get("logs"):
                    lines = job["logs"].split('\n')
                    if len(lines) > 20:
                        logs_preview = '\n'.join(lines[-20:]) + "\n... (truncated, use get_job_logs for full logs)"
                    else:
                        logs_preview = job["logs"]

                card["job"] = {
                    "id": job["id"],
                    "status": job["status"],
                    "error": job.get("error"),
                    "logs_preview": logs_preview,
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                }

        return card


@mcp.tool()
def start_card(card_id: str) -> dict:
    """
    Trigger agent work on a card.

    The card must be in 'todo' status and the repo must be ingested.
    This queues a job that will be picked up by an available runner matching
    the card's runner_type (any, claude-code, or gemini).

    Args:
        card_id: The card ID (UUID string) to start

    Returns the updated card with job info.
    """
    with _get_client() as client:
        response = client.post(f"/api/cards/{card_id}/start", json={})
        if response.status_code == 404:
            return {"error": f"Card {card_id} not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code != 200:
            return {"error": f"Failed to start card: {response.text}"}

        card = response.json()
        card["message"] = "Card started, job queued for runner"
        return card


@mcp.tool()
def get_job_logs(job_id: str, max_lines: int = 100) -> dict:
    """
    Fetch logs from a job run.

    Args:
        job_id: The job ID (UUID string)
        max_lines: Maximum number of log lines to return (default 100)

    Returns job status and logs.
    """
    with _get_client() as client:
        response = client.get(f"/api/jobs/{job_id}")
        if response.status_code == 404:
            return {"error": f"Job {job_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to get job: {response.text}"}

        job = response.json()

        logs = job.get("logs") or ""
        log_lines = logs.split('\n')
        total_lines = len(log_lines)

        if len(log_lines) > max_lines:
            log_lines = log_lines[-max_lines:]
            logs = '\n'.join(log_lines)
            truncated = True
        else:
            truncated = False

        return {
            "job_id": job["id"],
            "card_id": job["card_id"],
            "status": job["status"],
            "error": job.get("error"),
            "logs": logs,
            "total_lines": total_lines,
            "truncated": truncated,
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
        }


@mcp.resource("repos://list")
def repos_resource() -> str:
    """
    List of available repositories in LazyAF.

    Returns a formatted list of all repos with their status.
    """
    with _get_client() as client:
        response = client.get("/api/repos")
        if response.status_code != 200:
            return f"Error fetching repos: {response.text}"

        repos = response.json()

        if not repos:
            return "No repositories found. Use 'lazyaf ingest /path/to/repo' to add one."

        lines = ["Available Repositories:", ""]
        for repo in repos:
            status = "ingested" if repo.get("is_ingested") else "not ingested"
            lines.append(f"- {repo['name']} (ID: {repo['id']}) [{status}]")
            if repo.get("default_branch"):
                lines.append(f"  Default branch: {repo['default_branch']}")

        return '\n'.join(lines)


@mcp.tool()
def get_runner_status() -> dict:
    """
    Get the status of the runner pool.

    Shows how many runners are available, their types (claude-code or gemini),
    and how many jobs are queued.

    Returns pool status and list of active runners.
    """
    with _get_client() as client:
        # Get pool status
        status_response = client.get("/api/runners/status")
        if status_response.status_code != 200:
            return {"error": f"Failed to get runner status: {status_response.text}"}

        status = status_response.json()

        # Get runner list
        runners_response = client.get("/api/runners")
        runners = []
        if runners_response.status_code == 200:
            runners = runners_response.json()

        return {
            "pool": {
                "total_runners": status.get("total_runners", 0),
                "idle_runners": status.get("idle_runners", 0),
                "busy_runners": status.get("busy_runners", 0),
                "offline_runners": status.get("offline_runners", 0),
                "queued_jobs": status.get("queued_jobs", 0),
            },
            "runners": [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "type": r.get("runner_type", "unknown"),
                    "status": r.get("status"),
                }
                for r in runners
            ],
            "available_runner_types": ["any", "claude-code", "gemini"],
        }


# =============================================================================
# Pipeline Tools (Phase 9)
# =============================================================================

@mcp.tool()
def list_pipelines(repo_id: str = "") -> dict:
    """
    List pipelines, optionally filtered by repository.

    Args:
        repo_id: Optional repository ID to filter by. If empty, lists all pipelines.

    Returns list of pipelines in {"pipelines": [...]} format.
    """
    with _get_client() as client:
        if repo_id:
            response = client.get(f"/api/repos/{repo_id}/pipelines")
        else:
            response = client.get("/api/pipelines")

        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to list pipelines: {response.text}"}
        return {"pipelines": response.json()}


@mcp.tool()
def create_pipeline(
    repo_id: str,
    name: str,
    steps: list[dict],
    description: str = "",
    triggers: list[dict] = None
) -> dict:
    """
    Create a new pipeline for a repository.

    Args:
        repo_id: Repository ID
        name: Pipeline name (e.g., "PR Validation", "Deploy")
        steps: List of step definitions. Each step has:
            - name: Step name (e.g., "Lint", "Test", "Build")
            - type: "agent", "script", or "docker"
            - config: Type-specific config:
                - script: {"command": "npm test"}
                - docker: {"image": "node:20", "command": "npm build"}
                - agent: {"runner_type": "any", "title": "...", "description": "..."}
            - on_success: "next" | "stop" | "merge:{branch}" (default: "next")
            - on_failure: "next" | "stop" | "trigger:{card_id}" (default: "stop")
            - timeout: Seconds (default: 300)
        description: Optional description
        triggers: Optional list of automatic triggers. Each trigger has:
            - type: "card_complete" or "push"
            - config: Type-specific config:
                - card_complete: {"status": "in_review" | "done"}
                - push: {"branches": ["main", "dev"]}
            - enabled: true/false (default: true)
            - on_pass: Action on pipeline success - "nothing", "merge", "merge:{branch}"
            - on_fail: Action on pipeline failure - "nothing", "fail", "reject"

    Example steps:
    [
        {"name": "Lint", "type": "script", "config": {"command": "npm run lint"}},
        {"name": "Test", "type": "script", "config": {"command": "npm test"}, "on_failure": "stop"},
        {"name": "Build", "type": "docker", "config": {"image": "node:20", "command": "npm build"}}
    ]

    Example triggers:
    [
        {"type": "card_complete", "config": {"status": "in_review"}, "on_pass": "merge", "on_fail": "fail"}
    ]

    Returns the created pipeline.
    """
    if not steps:
        return {"error": "At least one step is required"}

    # Normalize steps with defaults
    normalized_steps = []
    for i, step in enumerate(steps):
        if not step.get("name"):
            return {"error": f"Step {i + 1} is missing 'name'"}
        if not step.get("type"):
            return {"error": f"Step '{step['name']}' is missing 'type'"}
        if step["type"] not in ["agent", "script", "docker"]:
            return {"error": f"Step '{step['name']}' has invalid type '{step['type']}'"}

        normalized_step = {
            "name": step["name"],
            "type": step["type"],
            "config": step.get("config", {}),
            "on_success": step.get("on_success", "next"),
            "on_failure": step.get("on_failure", "stop"),
            "timeout": step.get("timeout", 300),
        }
        normalized_steps.append(normalized_step)

    # Normalize triggers with defaults
    normalized_triggers = []
    if triggers:
        for trigger in triggers:
            if not trigger.get("type"):
                return {"error": "Each trigger must have a 'type' (card_complete or push)"}
            if trigger["type"] not in ["card_complete", "push"]:
                return {"error": f"Invalid trigger type '{trigger['type']}'"}

            normalized_trigger = {
                "type": trigger["type"],
                "config": trigger.get("config", {}),
                "enabled": trigger.get("enabled", True),
                "on_pass": trigger.get("on_pass", "nothing"),
                "on_fail": trigger.get("on_fail", "nothing"),
            }
            normalized_triggers.append(normalized_trigger)

    with _get_client() as client:
        payload = {
            "name": name,
            "description": description,
            "steps": normalized_steps,
        }
        if normalized_triggers:
            payload["triggers"] = normalized_triggers

        response = client.post(f"/api/repos/{repo_id}/pipelines", json=payload)
        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found"}
        if response.status_code not in (200, 201):
            return {"error": f"Failed to create pipeline: {response.text}"}

        pipeline = response.json()
        trigger_msg = f" with {len(normalized_triggers)} trigger(s)" if normalized_triggers else ""
        pipeline["message"] = f"Pipeline '{name}' created with {len(normalized_steps)} steps{trigger_msg}"
        return pipeline


@mcp.tool()
def get_pipeline(pipeline_id: str) -> dict:
    """
    Get full pipeline details including steps configuration.

    Args:
        pipeline_id: Pipeline ID (UUID string)

    Returns complete pipeline details.
    """
    with _get_client() as client:
        response = client.get(f"/api/pipelines/{pipeline_id}")
        if response.status_code == 404:
            return {"error": f"Pipeline {pipeline_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to get pipeline: {response.text}"}
        return response.json()


@mcp.tool()
def update_pipeline(
    pipeline_id: str,
    name: str = "",
    steps: list[dict] = None,
    description: str = None,
    triggers: list[dict] = None
) -> dict:
    """
    Update an existing pipeline.

    Args:
        pipeline_id: Pipeline ID to update
        name: New name (optional)
        steps: New steps list (optional, replaces existing)
        description: New description (optional)
        triggers: New triggers list (optional, replaces existing). Each trigger has:
            - type: "card_complete" or "push"
            - config: {"status": "in_review"} or {"branches": ["main"]}
            - enabled: true/false
            - on_pass: "nothing" | "merge" | "merge:{branch}"
            - on_fail: "nothing" | "fail" | "reject"

    Returns updated pipeline.
    """
    payload = {}
    if name:
        payload["name"] = name
    if steps is not None:
        payload["steps"] = steps
    if description is not None:
        payload["description"] = description
    if triggers is not None:
        payload["triggers"] = triggers

    if not payload:
        return {"error": "No fields to update"}

    with _get_client() as client:
        response = client.patch(f"/api/pipelines/{pipeline_id}", json=payload)
        if response.status_code == 404:
            return {"error": f"Pipeline {pipeline_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to update pipeline: {response.text}"}

        pipeline = response.json()
        pipeline["message"] = "Pipeline updated successfully"
        return pipeline


@mcp.tool()
def delete_pipeline(pipeline_id: str) -> dict:
    """
    Delete a pipeline and all its runs.

    Args:
        pipeline_id: Pipeline ID to delete

    Returns success status.
    """
    with _get_client() as client:
        response = client.delete(f"/api/pipelines/{pipeline_id}")
        if response.status_code == 404:
            return {"error": f"Pipeline {pipeline_id} not found"}
        if response.status_code not in (200, 204):
            return {"error": f"Failed to delete pipeline: {response.text}"}
        return {"success": True, "message": "Pipeline deleted"}


@mcp.tool()
def run_pipeline(pipeline_id: str) -> dict:
    """
    Trigger a pipeline run.

    The repo must be ingested and have at least one step defined.
    Runs execute steps sequentially, following on_success/on_failure branching.

    Args:
        pipeline_id: Pipeline to run

    Returns the pipeline run details with run_id for monitoring.
    """
    with _get_client() as client:
        response = client.post(f"/api/pipelines/{pipeline_id}/run", json={})
        if response.status_code == 404:
            return {"error": f"Pipeline {pipeline_id} not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code not in (200, 201):
            return {"error": f"Failed to run pipeline: {response.text}"}

        run = response.json()
        run["message"] = f"Pipeline started with {run.get('steps_total', 0)} steps"
        return run


@mcp.tool()
def get_pipeline_run(run_id: str) -> dict:
    """
    Get pipeline run status with step-by-step details.

    Args:
        run_id: Pipeline run ID

    Returns run status, current step, completed steps, and each step's status/logs.
    """
    with _get_client() as client:
        response = client.get(f"/api/pipeline-runs/{run_id}")
        if response.status_code == 404:
            return {"error": f"Pipeline run {run_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to get pipeline run: {response.text}"}
        return response.json()


@mcp.tool()
def list_pipeline_runs(
    pipeline_id: str = "",
    status: str = "",
    limit: int = 20
) -> dict:
    """
    List pipeline runs with optional filters.

    Args:
        pipeline_id: Filter by pipeline (optional)
        status: Filter by status: pending, running, passed, failed, cancelled (optional)
        limit: Maximum results (default 20)

    Returns list of pipeline runs in {"runs": [...]} format.
    """
    with _get_client() as client:
        params = {"limit": limit}
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        if status:
            params["status"] = status

        response = client.get("/api/pipeline-runs", params=params)
        if response.status_code != 200:
            return {"error": f"Failed to list runs: {response.text}"}
        return {"runs": response.json()}


@mcp.tool()
def cancel_pipeline_run(run_id: str) -> dict:
    """
    Cancel a running pipeline.

    Args:
        run_id: Pipeline run ID to cancel

    Returns updated run status.
    """
    with _get_client() as client:
        response = client.post(f"/api/pipeline-runs/{run_id}/cancel")
        if response.status_code == 404:
            return {"error": f"Pipeline run {run_id} not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code != 200:
            return {"error": f"Failed to cancel run: {response.text}"}

        run = response.json()
        run["message"] = "Pipeline run cancelled"
        return run


@mcp.tool()
def get_step_logs(run_id: str, step_index: int) -> dict:
    """
    Get logs for a specific step in a pipeline run.

    Args:
        run_id: Pipeline run ID
        step_index: Step index (0-based)

    Returns step logs and status.
    """
    with _get_client() as client:
        response = client.get(f"/api/pipeline-runs/{run_id}/steps/{step_index}/logs")
        if response.status_code == 404:
            return {"error": f"Step not found (run_id={run_id}, step={step_index})"}
        if response.status_code != 200:
            return {"error": f"Failed to get step logs: {response.text}"}
        return response.json()


# =============================================================================
# Card Action Tools
# =============================================================================

@mcp.tool()
def approve_card(card_id: str, target_branch: str = "") -> dict:
    """
    Approve a card and merge its branch to the target branch.

    The card must be in 'in_review' status with a branch.
    This merges the card's changes and marks it as 'done'.

    Args:
        card_id: The card ID to approve
        target_branch: Branch to merge into (default: repo's default branch)

    Returns the updated card and merge result.
    """
    with _get_client() as client:
        params = {}
        if target_branch:
            params["target_branch"] = target_branch

        response = client.post(f"/api/cards/{card_id}/approve", params=params)
        if response.status_code == 404:
            return {"error": f"Card {card_id} not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code != 200:
            return {"error": f"Failed to approve card: {response.text}"}

        result = response.json()
        result["message"] = "Card approved and merged"
        return result


@mcp.tool()
def reject_card(card_id: str) -> dict:
    """
    Reject a card back to todo status.

    Clears the card's branch and resets it for re-work.
    The card must be in 'in_review' or 'failed' status.

    Args:
        card_id: The card ID to reject

    Returns the updated card.
    """
    with _get_client() as client:
        response = client.post(f"/api/cards/{card_id}/reject")
        if response.status_code == 404:
            return {"error": f"Card {card_id} not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code != 200:
            return {"error": f"Failed to reject card: {response.text}"}

        card = response.json()
        card["message"] = "Card rejected back to todo"
        return card


@mcp.tool()
def retry_card(card_id: str) -> dict:
    """
    Retry a failed card.

    Requeues the card for another attempt by a runner.
    The card must be in 'failed' status.

    Args:
        card_id: The card ID to retry

    Returns the updated card with new job info.
    """
    with _get_client() as client:
        response = client.post(f"/api/cards/{card_id}/retry")
        if response.status_code == 404:
            return {"error": f"Card {card_id} not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code != 200:
            return {"error": f"Failed to retry card: {response.text}"}

        card = response.json()
        card["message"] = "Card retried, new job queued"
        return card


@mcp.tool()
def update_card(
    card_id: str,
    title: str = "",
    description: str = "",
    runner_type: str = "",
    step_type: str = "",
    command: str = "",
    image: str = ""
) -> dict:
    """
    Update an existing card's details.

    Only provided fields are updated. Card must be in 'todo' status
    to change step_type or runner_type.

    Args:
        card_id: The card ID to update
        title: New title (optional)
        description: New description (optional)
        runner_type: New runner type (optional)
        step_type: New step type (optional)
        command: New command for script/docker steps (optional)
        image: New docker image (optional)

    Returns the updated card.
    """
    payload = {}
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description
    if runner_type:
        payload["runner_type"] = runner_type
    if step_type:
        payload["step_type"] = step_type

    # Build step_config if command or image provided
    if command or image:
        step_config = {}
        if command:
            step_config["command"] = command
        if image:
            step_config["image"] = image
        payload["step_config"] = step_config

    if not payload:
        return {"error": "No fields to update"}

    with _get_client() as client:
        response = client.patch(f"/api/cards/{card_id}", json=payload)
        if response.status_code == 404:
            return {"error": f"Card {card_id} not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code != 200:
            return {"error": f"Failed to update card: {response.text}"}

        card = response.json()
        card["message"] = "Card updated successfully"
        return card


@mcp.tool()
def delete_card(card_id: str) -> dict:
    """
    Delete a card.

    The card and any associated jobs will be removed.

    Args:
        card_id: The card ID to delete

    Returns success status.
    """
    with _get_client() as client:
        response = client.delete(f"/api/cards/{card_id}")
        if response.status_code == 404:
            return {"error": f"Card {card_id} not found"}
        if response.status_code not in (200, 204):
            return {"error": f"Failed to delete card: {response.text}"}
        return {"success": True, "message": "Card deleted"}


# =============================================================================
# Agent Files Tools
# =============================================================================

@mcp.tool()
def list_agent_files() -> dict:
    """
    List all platform agent files.

    Agent files contain reusable prompt templates that can be
    attached to cards or pipeline steps.

    Returns list of agent files in {"agent_files": [...]} format.
    """
    with _get_client() as client:
        response = client.get("/api/agent-files")
        if response.status_code != 200:
            return {"error": f"Failed to list agent files: {response.text}"}
        return {"agent_files": response.json()}


@mcp.tool()
def get_agent_file(agent_file_id: str) -> dict:
    """
    Get an agent file's full content.

    Args:
        agent_file_id: The agent file ID

    Returns the agent file with name, description, and content.
    """
    with _get_client() as client:
        response = client.get(f"/api/agent-files/{agent_file_id}")
        if response.status_code == 404:
            return {"error": f"Agent file {agent_file_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to get agent file: {response.text}"}
        return response.json()


@mcp.tool()
def create_agent_file(
    name: str,
    content: str,
    description: str = ""
) -> dict:
    """
    Create a new platform agent file.

    Agent files are reusable prompt templates. Use {{title}} and {{description}}
    placeholders which will be replaced with card/step values.

    Args:
        name: Agent name (e.g., "test-fixer", "code-reviewer")
        content: The prompt template content
        description: Optional description of what this agent does

    Returns the created agent file.
    """
    if not name:
        return {"error": "name is required"}
    if not content:
        return {"error": "content is required"}

    with _get_client() as client:
        payload = {
            "name": name,
            "content": content,
        }
        if description:
            payload["description"] = description

        response = client.post("/api/agent-files", json=payload)
        if response.status_code not in (200, 201):
            return {"error": f"Failed to create agent file: {response.text}"}

        agent_file = response.json()
        agent_file["message"] = f"Agent file '{name}' created"
        return agent_file


@mcp.tool()
def update_agent_file(
    agent_file_id: str,
    name: str = "",
    content: str = "",
    description: str = None
) -> dict:
    """
    Update an existing agent file.

    Args:
        agent_file_id: The agent file ID to update
        name: New name (optional)
        content: New content (optional)
        description: New description (optional, use empty string to clear)

    Returns the updated agent file.
    """
    payload = {}
    if name:
        payload["name"] = name
    if content:
        payload["content"] = content
    if description is not None:
        payload["description"] = description

    if not payload:
        return {"error": "No fields to update"}

    with _get_client() as client:
        response = client.patch(f"/api/agent-files/{agent_file_id}", json=payload)
        if response.status_code == 404:
            return {"error": f"Agent file {agent_file_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to update agent file: {response.text}"}

        agent_file = response.json()
        agent_file["message"] = "Agent file updated"
        return agent_file


@mcp.tool()
def delete_agent_file(agent_file_id: str) -> dict:
    """
    Delete an agent file.

    Args:
        agent_file_id: The agent file ID to delete

    Returns success status.
    """
    with _get_client() as client:
        response = client.delete(f"/api/agent-files/{agent_file_id}")
        if response.status_code == 404:
            return {"error": f"Agent file {agent_file_id} not found"}
        if response.status_code not in (200, 204):
            return {"error": f"Failed to delete agent file: {response.text}"}
        return {"success": True, "message": "Agent file deleted"}


# =============================================================================
# Git/Branch Tools
# =============================================================================

@mcp.tool()
def list_branches(repo_id: str) -> dict:
    """
    List all branches in a repository.

    Args:
        repo_id: The repository ID

    Returns list of branches with their commit info and status.
    """
    with _get_client() as client:
        response = client.get(f"/api/repos/{repo_id}/branches")
        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to list branches: {response.text}"}
        return response.json()


@mcp.tool()
def get_diff(
    repo_id: str,
    head_branch: str,
    base_branch: str = ""
) -> dict:
    """
    Get the diff between two branches.

    Args:
        repo_id: The repository ID
        head_branch: The branch with changes
        base_branch: The branch to compare against (default: repo's default branch)

    Returns file diffs with additions, deletions, and patch content.
    """
    with _get_client() as client:
        params = {"head_branch": head_branch}
        if base_branch:
            params["base_branch"] = base_branch

        response = client.get(f"/api/repos/{repo_id}/diff", params=params)
        if response.status_code == 404:
            return {"error": f"Repo or branch not found"}
        if response.status_code == 400:
            error_detail = response.json().get("detail", "Bad request")
            return {"error": error_detail}
        if response.status_code != 200:
            return {"error": f"Failed to get diff: {response.text}"}
        return response.json()


# =============================================================================
# Repo-Defined Assets (.lazyaf) Tools
# =============================================================================

@mcp.tool()
def list_repo_agents(repo_id: str, branch: str = "") -> dict:
    """
    List agents defined in a repository's .lazyaf/agents/ directory.

    These are repo-specific prompt templates that can be referenced
    in cards and pipeline steps.

    Args:
        repo_id: The repository ID
        branch: Branch to read from (default: repo's default branch)

    Returns list of repo-defined agents in {"agents": [...]} format.
    """
    with _get_client() as client:
        params = {}
        if branch:
            params["branch"] = branch

        response = client.get(f"/api/repos/{repo_id}/lazyaf/agents", params=params)
        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found or not ingested"}
        if response.status_code != 200:
            return {"error": f"Failed to list repo agents: {response.text}"}
        return {"agents": response.json()}


@mcp.tool()
def list_repo_pipelines(repo_id: str, branch: str = "") -> dict:
    """
    List pipelines defined in a repository's .lazyaf/pipelines/ directory.

    These are repo-specific pipeline definitions in YAML format.

    Args:
        repo_id: The repository ID
        branch: Branch to read from (default: repo's default branch)

    Returns list of repo-defined pipelines in {"pipelines": [...]} format.
    """
    with _get_client() as client:
        params = {}
        if branch:
            params["branch"] = branch

        response = client.get(f"/api/repos/{repo_id}/lazyaf/pipelines", params=params)
        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found or not ingested"}
        if response.status_code != 200:
            return {"error": f"Failed to list repo pipelines: {response.text}"}
        return {"pipelines": response.json()}
