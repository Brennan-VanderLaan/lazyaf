"""
Root conftest.py - Shared fixtures for all test types.

This file is automatically loaded by pytest and provides:
- Database session fixtures for integration tests
- FastAPI test client
- Factory registration
- Common test utilities
"""
import sys
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Add backend to path for imports - handle both local and Docker environments
backend_path = Path(__file__).parent.parent / "backend"
if not backend_path.exists():
    # Running inside Docker container where backend is at /app
    backend_path = Path("/app")
if backend_path.exists():
    sys.path.insert(0, str(backend_path))

from app.database import Base, get_db
from app.main import app


# -----------------------------------------------------------------------------
# Database Fixtures
# -----------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def async_engine():
    """Create a test database engine."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional database session for tests.

    Each test gets a fresh session that is rolled back after the test.
    """
    async_session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with async_session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP client for API testing.

    This client is configured to use the test database session.
    """
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# Marker-based fixtures
# -----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mark_test(request):
    """Automatically apply markers based on test location."""
    if "unit" in str(request.fspath):
        request.applymarker(pytest.mark.unit)
    elif "integration" in str(request.fspath):
        request.applymarker(pytest.mark.integration)
    elif "demos" in str(request.fspath):
        request.applymarker(pytest.mark.demo)


# -----------------------------------------------------------------------------
# Runner Pool and Job Queue Fixtures
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def clean_runner_pool():
    """Clean runner pool state before and after each test.

    This fixture ensures tests have a fresh runner pool state and
    cleans up afterward to prevent test pollution.
    """
    from app.services.runner_pool import runner_pool

    # Clear before
    runner_pool._runners = {}
    runner_pool._running = False
    runner_pool._worker_task = None

    yield runner_pool

    # Clear after
    if runner_pool._running:
        await runner_pool.stop()
    runner_pool._runners = {}
    runner_pool._running = False
    runner_pool._worker_task = None


@pytest_asyncio.fixture
async def clean_job_queue():
    """Clean job queue state before and after each test.

    This fixture ensures tests have a fresh job queue state.
    """
    from app.services.job_queue import job_queue

    # Clear before
    job_queue._pending = {}
    job_queue._jobs = []

    yield job_queue

    # Clear after
    job_queue._pending = {}
    job_queue._jobs = []


# -----------------------------------------------------------------------------
# Git Server Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def temp_git_repos_dir(tmp_path):
    """Create a temporary git repos directory for tests."""
    git_dir = tmp_path / "git_repos"
    git_dir.mkdir()
    return git_dir


@pytest_asyncio.fixture
async def clean_git_repos(temp_git_repos_dir):
    """Clean git repos directory before and after each test.

    This fixture ensures tests have a fresh git storage state and
    cleans up any repos created during tests.

    Uses a temp directory to avoid path issues on Windows.
    """
    import shutil
    from app.services.git_server import git_repo_manager

    # Store original repos_dir
    original_repos_dir = git_repo_manager.repos_dir
    original_initialized = git_repo_manager._initialized

    # Override with temp directory
    git_repo_manager.repos_dir = temp_git_repos_dir
    git_repo_manager._initialized = True

    yield git_repo_manager

    # Restore original settings
    git_repo_manager.repos_dir = original_repos_dir
    git_repo_manager._initialized = original_initialized

    # Clean up temp dir (handled by pytest tmp_path fixture)


@pytest.fixture(scope="session", autouse=True)
def cleanup_git_repos_after_session():
    """Clean up git repos directory after the entire test session."""
    import shutil
    from pathlib import Path

    yield

    # Clean up after all tests - handle both local and Docker environments
    git_repos_dir = Path(__file__).parent.parent / "backend" / "git_repos"
    if not git_repos_dir.parent.exists():
        git_repos_dir = Path("/app") / "git_repos"
    if git_repos_dir.exists():
        shutil.rmtree(git_repos_dir)


# -----------------------------------------------------------------------------
# Repo Fixtures
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def repo(client):
    """Create a test repository for tests that need one."""
    from shared.factories import repo_create_payload

    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="test-repo"),
    )
    assert response.status_code == 201, f"Failed to create repo: {response.text}"
    return response.json()


@pytest_asyncio.fixture
async def ingested_repo(client, clean_git_repos):
    """Create and ingest a test repository."""
    import tempfile
    import subprocess
    from pathlib import Path

    # Create a temporary git repo
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test-repo"
        repo_path.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True, capture_output=True)

        # Create a file and commit
        (repo_path / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True, capture_output=True)

        # Ingest it
        response = await client.post(
            "/api/repos/ingest",
            json={"path": str(repo_path), "name": "ingested-test-repo"},
        )
        assert response.status_code == 201, f"Failed to ingest repo: {response.text}"
        ingest_data = response.json()

        # Get the full repo
        repo_response = await client.get(f"/api/repos/{ingest_data['id']}")
        return repo_response.json()


# -----------------------------------------------------------------------------
# Utility Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    """Required for pytest-asyncio compatibility."""
    return "asyncio"
