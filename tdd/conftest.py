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

# Add backend to path for imports
backend_path = Path(__file__).parent.parent / "backend"
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
    # Drain the asyncio queue
    while True:
        try:
            job_queue._queue.get_nowait()
        except Exception:
            break

    yield job_queue

    # Clear after
    job_queue._pending = {}
    while True:
        try:
            job_queue._queue.get_nowait()
        except Exception:
            break


# -----------------------------------------------------------------------------
# Utility Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    """Required for pytest-asyncio compatibility."""
    return "asyncio"
