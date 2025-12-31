"""
API request/response factories.

These factories create dictionaries suitable for API request payloads
and expected response structures. They help keep tests DRY and maintainable.
"""
from datetime import datetime
from typing import Any

from faker import Faker

from .base import generate_uuid

fake = Faker()


# -----------------------------------------------------------------------------
# Repo API Factories
# -----------------------------------------------------------------------------

def repo_create_payload(
    name: str | None = None,
    remote_url: str | None = None,
    default_branch: str = "main",
) -> dict[str, Any]:
    """Create a payload for POST /api/repos.

    Note: path field is deprecated. Repos now use internal git storage.
    """
    return {
        "name": name or fake.word().capitalize() + "Repo",
        "remote_url": remote_url,
        "default_branch": default_branch,
    }


def repo_update_payload(**kwargs) -> dict[str, Any]:
    """Create a payload for PATCH /api/repos/{id}.

    Only includes fields that are explicitly provided.
    Note: path field is deprecated. Repos now use internal git storage.
    """
    valid_fields = {"name", "remote_url", "default_branch"}
    return {k: v for k, v in kwargs.items() if k in valid_fields}


# -----------------------------------------------------------------------------
# Card API Factories
# -----------------------------------------------------------------------------

def card_create_payload(
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a payload for POST /api/repos/{repo_id}/cards."""
    return {
        "title": title or fake.sentence(nb_words=4).rstrip("."),
        "description": description or fake.paragraph(nb_sentences=2),
    }


def card_update_payload(**kwargs) -> dict[str, Any]:
    """Create a payload for PATCH /api/cards/{id}.

    Only includes fields that are explicitly provided.
    """
    valid_fields = {"title", "description", "status"}
    return {k: v for k, v in kwargs.items() if k in valid_fields}


# -----------------------------------------------------------------------------
# Expected Response Structures
# -----------------------------------------------------------------------------

def expect_repo_response(
    id: str | None = None,
    name: str | None = None,
    is_ingested: bool | None = None,
    internal_git_url: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for repo endpoints.

    Used for partial matching in assertions.
    Note: path field is deprecated. Use internal_git_url instead.
    """
    response = {}
    if id is not None:
        response["id"] = id
    if name is not None:
        response["name"] = name
    if is_ingested is not None:
        response["is_ingested"] = is_ingested
    if internal_git_url is not None:
        response["internal_git_url"] = internal_git_url
    response.update(kwargs)
    return response


def expect_card_response(
    id: str | None = None,
    title: str | None = None,
    status: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for card endpoints."""
    response = {}
    if id is not None:
        response["id"] = id
    if title is not None:
        response["title"] = title
    if status is not None:
        response["status"] = status
    response.update(kwargs)
    return response


def expect_job_response(
    id: str | None = None,
    status: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for job endpoints."""
    response = {}
    if id is not None:
        response["id"] = id
    if status is not None:
        response["status"] = status
    response.update(kwargs)
    return response


def expect_error_response(detail: str) -> dict[str, Any]:
    """Create expected error response structure."""
    return {"detail": detail}


# -----------------------------------------------------------------------------
# Git/Ingest API Factories
# -----------------------------------------------------------------------------

def repo_ingest_payload(
    name: str | None = None,
    remote_url: str | None = None,
    default_branch: str = "main",
) -> dict[str, Any]:
    """Create a payload for POST /api/repos/ingest.

    This creates a repo and initializes internal git storage.
    Unlike repo_create_payload, this does not include 'path' as
    ingested repos use internal git storage.
    """
    return {
        "name": name or fake.word().capitalize() + "Repo",
        "remote_url": remote_url,
        "default_branch": default_branch,
    }


def expect_ingest_response(
    id: str | None = None,
    name: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for ingest endpoint.

    Response includes:
    - id: The repo UUID
    - name: The repo name
    - internal_git_url: Relative path like /git/{id}.git
    - clone_url: Full URL for cloning
    """
    response = {}
    if id is not None:
        response["id"] = id
    if name is not None:
        response["name"] = name
    response.update(kwargs)
    return response


def expect_clone_url_response(
    clone_url: str | None = None,
    is_ingested: bool | None = None,
) -> dict[str, Any]:
    """Create expected response structure for clone-url endpoint."""
    response = {}
    if clone_url is not None:
        response["clone_url"] = clone_url
    if is_ingested is not None:
        response["is_ingested"] = is_ingested
    return response


# -----------------------------------------------------------------------------
# Pipeline API Factories
# -----------------------------------------------------------------------------

def pipeline_step_payload(
    name: str | None = None,
    step_type: str = "script",
    config: dict[str, Any] | None = None,
    on_success: str = "next",
    on_failure: str = "stop",
    timeout: int = 300,
) -> dict[str, Any]:
    """Create a pipeline step definition.

    Args:
        name: Step name (e.g., "Lint", "Test", "Build")
        step_type: "script", "docker", or "agent"
        config: Type-specific configuration
        on_success: Action on success - "next", "stop", "merge:{branch}", "trigger:{card_id}"
        on_failure: Action on failure - "next", "stop", "trigger:{card_id}"
        timeout: Timeout in seconds
    """
    return {
        "name": name or fake.word().capitalize() + " Step",
        "type": step_type,
        "config": config or {},
        "on_success": on_success,
        "on_failure": on_failure,
        "timeout": timeout,
    }


def pipeline_create_payload(
    name: str | None = None,
    description: str | None = None,
    steps: list[dict[str, Any]] | None = None,
    is_template: bool = False,
) -> dict[str, Any]:
    """Create a payload for POST /api/repos/{repo_id}/pipelines.

    Args:
        name: Pipeline name
        description: Pipeline description
        steps: List of step definitions (use pipeline_step_payload to create)
        is_template: Whether this is a template pipeline
    """
    return {
        "name": name or fake.word().capitalize() + " Pipeline",
        "description": description or fake.sentence(),
        "steps": steps or [],
        "is_template": is_template,
    }


def pipeline_update_payload(**kwargs) -> dict[str, Any]:
    """Create a payload for PATCH /api/pipelines/{id}.

    Only includes fields that are explicitly provided.
    """
    valid_fields = {"name", "description", "steps", "is_template"}
    return {k: v for k, v in kwargs.items() if k in valid_fields}


def pipeline_run_create_payload(
    trigger_type: str = "manual",
    trigger_ref: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a payload for POST /api/pipelines/{id}/run.

    Args:
        trigger_type: How the run was triggered - "manual", "webhook", "card", "push", "schedule"
        trigger_ref: Optional reference (e.g., commit SHA, PR number)
        params: Optional parameters to pass to steps
    """
    payload = {"trigger_type": trigger_type}
    if trigger_ref is not None:
        payload["trigger_ref"] = trigger_ref
    if params is not None:
        payload["params"] = params
    return payload


# -----------------------------------------------------------------------------
# Pipeline Expected Response Structures
# -----------------------------------------------------------------------------

def expect_pipeline_response(
    id: str | None = None,
    name: str | None = None,
    repo_id: str | None = None,
    is_template: bool | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for pipeline endpoints."""
    response = {}
    if id is not None:
        response["id"] = id
    if name is not None:
        response["name"] = name
    if repo_id is not None:
        response["repo_id"] = repo_id
    if is_template is not None:
        response["is_template"] = is_template
    response.update(kwargs)
    return response


def expect_pipeline_run_response(
    id: str | None = None,
    pipeline_id: str | None = None,
    status: str | None = None,
    trigger_type: str | None = None,
    current_step: int | None = None,
    steps_completed: int | None = None,
    steps_total: int | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for pipeline run endpoints."""
    response = {}
    if id is not None:
        response["id"] = id
    if pipeline_id is not None:
        response["pipeline_id"] = pipeline_id
    if status is not None:
        response["status"] = status
    if trigger_type is not None:
        response["trigger_type"] = trigger_type
    if current_step is not None:
        response["current_step"] = current_step
    if steps_completed is not None:
        response["steps_completed"] = steps_completed
    if steps_total is not None:
        response["steps_total"] = steps_total
    response.update(kwargs)
    return response


def expect_step_run_response(
    id: str | None = None,
    pipeline_run_id: str | None = None,
    step_index: int | None = None,
    step_name: str | None = None,
    status: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for step run endpoints."""
    response = {}
    if id is not None:
        response["id"] = id
    if pipeline_run_id is not None:
        response["pipeline_run_id"] = pipeline_run_id
    if step_index is not None:
        response["step_index"] = step_index
    if step_name is not None:
        response["step_name"] = step_name
    if status is not None:
        response["status"] = status
    response.update(kwargs)
    return response
