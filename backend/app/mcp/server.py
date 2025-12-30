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
    instructions="""LazyAF is a visual orchestrator for AI agents to handle feature development.

Use these tools to:
- List and inspect repositories
- Create and manage cards (feature requests)
- Start agent work on cards
- Monitor job progress and logs

Typical workflow:
1. list_repos() to see available repositories
2. list_cards(repo_id) to see existing work items
3. create_card(repo_id, title, description) to add new work
4. start_card(card_id) to trigger AI agent work
5. get_job_logs(job_id) to monitor progress
"""
)


def _get_client() -> httpx.Client:
    """Get an HTTP client for backend communication."""
    return httpx.Client(base_url=BACKEND_URL, timeout=30.0)


@mcp.tool()
def list_repos() -> list[dict]:
    """
    List all repositories in LazyAF.

    Returns a list of repos with their ID, name, ingest status, and default branch.
    Only ingested repos can have cards started on them.
    """
    with _get_client() as client:
        response = client.get("/api/repos")
        if response.status_code != 200:
            return {"error": f"Failed to list repos: {response.text}"}
        return response.json()


@mcp.tool()
def list_cards(repo_id: str) -> list[dict]:
    """
    List all cards for a repository.

    Args:
        repo_id: The repository ID (UUID string)

    Returns cards with status, branch, and job info.
    """
    with _get_client() as client:
        response = client.get(f"/api/repos/{repo_id}/cards")
        if response.status_code == 404:
            return {"error": f"Repo {repo_id} not found"}
        if response.status_code != 200:
            return {"error": f"Failed to list cards: {response.text}"}
        return response.json()


@mcp.tool()
def create_card(repo_id: str, title: str, description: str = "") -> dict:
    """
    Create a new card in a repository.

    Args:
        repo_id: The repository ID (UUID string)
        title: Card title describing the feature or task
        description: Detailed description of what needs to be done

    Returns the created card details.
    """
    with _get_client() as client:
        response = client.post(
            f"/api/repos/{repo_id}/cards",
            json={"title": title, "description": description}
        )
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
    This queues a job that will be picked up by an available runner.

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
