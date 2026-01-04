"""
Integration tests for health check and root endpoints.

These tests verify the basic API functionality and availability.
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from shared.assertions import assert_status_code, assert_json_contains


class TestHealthEndpoint:
    """Tests for GET /health endpoint."""

    async def test_health_returns_ok(self, client):
        """Health endpoint returns OK status."""
        response = await client.get("/health")
        assert_status_code(response, 200)
        assert_json_contains(response, {"status": "ok"})

    async def test_health_includes_app_name(self, client):
        """Health endpoint includes app name."""
        response = await client.get("/health")
        result = response.json()
        assert "app" in result
        assert result["app"] == "LazyAF"


class TestRootEndpoint:
    """Tests for GET / endpoint."""

    async def test_root_returns_welcome(self, client):
        """Root endpoint returns welcome message."""
        response = await client.get("/")
        assert_status_code(response, 200)
        result = response.json()
        assert "message" in result
        assert "LazyAF" in result["message"]

    async def test_root_includes_docs_link(self, client):
        """Root endpoint includes link to docs."""
        response = await client.get("/")
        result = response.json()
        assert "docs" in result
        assert result["docs"] == "/docs"


class TestOpenAPISchema:
    """Tests for OpenAPI documentation endpoints."""

    async def test_openapi_json_available(self, client):
        """OpenAPI schema is available at /openapi.json."""
        response = await client.get("/openapi.json")
        assert_status_code(response, 200)
        schema = response.json()
        assert "openapi" in schema
        assert "paths" in schema

    async def test_openapi_includes_all_paths(self, client):
        """OpenAPI schema includes all expected paths."""
        response = await client.get("/openapi.json")
        paths = response.json()["paths"]

        expected_paths = [
            "/api/repos",
            "/api/repos/{repo_id}",
            "/api/repos/{repo_id}/cards",
            "/api/cards/{card_id}",
            "/api/jobs/{job_id}",
            # Note: Runners now use WebSocket at /ws/runner (Phase 12.6)
        ]

        for path in expected_paths:
            assert path in paths, f"Expected path {path} not found in OpenAPI schema"

    async def test_docs_page_available(self, client):
        """Swagger UI docs page is available."""
        response = await client.get("/docs")
        # Swagger UI returns HTML
        assert_status_code(response, 200)
        assert "text/html" in response.headers.get("content-type", "")
