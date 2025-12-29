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
    path: str | None = None,
    remote_url: str | None = None,
    default_branch: str = "main",
) -> dict[str, Any]:
    """Create a payload for POST /api/repos."""
    return {
        "name": name or fake.word().capitalize() + "Repo",
        "path": path or f"/repos/{fake.slug()}",
        "remote_url": remote_url,
        "default_branch": default_branch,
    }


def repo_update_payload(**kwargs) -> dict[str, Any]:
    """Create a payload for PATCH /api/repos/{id}.

    Only includes fields that are explicitly provided.
    """
    valid_fields = {"name", "path", "remote_url", "default_branch"}
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
    path: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create expected response structure for repo endpoints.

    Used for partial matching in assertions.
    """
    response = {}
    if id is not None:
        response["id"] = id
    if name is not None:
        response["name"] = name
    if path is not None:
        response["path"] = path
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
