"""
Root conftest.py - Shared fixtures for all test types.

This file is automatically loaded by pytest and provides:
- Database session fixtures for integration tests
- FastAPI test client
- Factory registration
- Common test utilities
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
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


def pytest_configure(config):
    """Register the local_exec marker (12.2-INT fix 13)."""
    config.addinivalue_line(
        "markers",
        "local_exec: test exercises the real local execution path (Docker); "
        "exempt from the T1 no-docker guard and the legacy-by-default router "
        "patch on the global pipeline executor",
    )
    # 12.2.6 test tie-back (pinned contract #5): the marker name is registered
    # HERE so plugin-less invocations (plain `uv run pytest ../tdd`, without
    # `-p runner_common.pytest_lazyaf`) collect annotated tests warning-free
    # under --strict-markers. The plugin registers the same line when loaded
    # (duplicate registration is harmless); outcome COLLECTION only happens
    # with the plugin loaded AND LAZYAF_TEST_RESULTS_PATH set.
    config.addinivalue_line(
        "markers",
        "lazyaf_test_id(id): map this test's outcome to the LazyAF TestRef "
        "with this stable id (Phase 12.2.6 test tie-back)",
    )


# -----------------------------------------------------------------------------
# T1 isolation (12.2-INT fix 13): the no-Docker tier must stay no-Docker
# -----------------------------------------------------------------------------

def _is_t2_docker_tier(request) -> bool:
    """The Docker-real T2 subtree (tdd/integration/services, per
    scripts/run_tier.py) is exempt from the T1 guards by location."""
    return "/integration/services" in str(request.fspath).replace("\\", "/")


@pytest.fixture(autouse=True)
def _t1_no_docker_guard(request, monkeypatch):
    """T1 must pass with Docker STOPPED.

    Any test that is neither marked ``local_exec`` nor part of the Docker
    tier (tdd/integration/services) fails loudly the moment it tries to
    construct a real docker client - the guard that catches the local
    execution path leaking into the no-Docker tier.
    """
    if request.node.get_closest_marker("local_exec") or _is_t2_docker_tier(request):
        yield
        return

    import docker as docker_sdk

    def _no_docker_in_t1(*args, **kwargs):
        raise AssertionError(
            "A real docker client was constructed in a test that is not "
            "marked 'local_exec' and is outside tdd/integration/services. "
            "T1 must pass with Docker stopped: route the step legacy, inject "
            "a fake client, or mark the test with @pytest.mark.local_exec."
        )

    monkeypatch.setattr(docker_sdk, "from_env", _no_docker_in_t1)
    monkeypatch.setattr(docker_sdk, "DockerClient", _no_docker_in_t1)
    yield


class _T1StubLocalExecutor:
    """Docker-free stand-in for LocalExecutor on the GLOBAL executor (T1).

    Yields the same event stream shape as the real
    app.services.execution.local_executor.LocalExecutor (status -> log ->
    result) and reports a clean success, so an API-tier test that starts a
    pipeline drives the REAL local dispatch path - router decision, workspace
    lifecycle, event consumer, StepRun persistence, WS publishes, run
    continuation - without ever constructing a docker client.

    Stock-image semantics: no control-layer label, no missing images, so
    every step takes the plain stdout reporting mode.
    """

    def __init__(self):
        self.calls: list[tuple[dict, dict]] = []

    async def image_supports_control_layer(self, image: str) -> bool:
        return False

    async def find_missing_images(self, images) -> list[str]:
        return []

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        for event in (
            {"type": "status", "status": "preparing"},
            {"type": "status", "status": "running"},
            {"type": "log", "line": f"[t1-stub] {step_config.get('command', '')}"},
            {"type": "result", "status": "completed", "exit_code": 0},
        ):
            await asyncio.sleep(0)
            yield event

    async def cancel_step(self, execution_key):
        return False

    async def cancel_all(self):
        return None

    def reset(self):
        self.calls.clear()


class _T1StubWorkspaceService:
    """Docker-free stand-in for WorkspaceService on the GLOBAL executor (T1).

    Honors the acquire/release/cleanup lifecycle the executor drives, but
    never creates a named volume or touches the docker SDK.
    """

    def __init__(self):
        self.ops: list[tuple] = []
        self._workspaces: dict = {}

    async def get_or_create(self, db, pipeline_run_id, repo_id, branch, commit_sha):
        self.ops.append(
            ("get_or_create", pipeline_run_id, repo_id, branch, commit_sha)
        )
        ws = self._workspaces.get(pipeline_run_id)
        if ws is None:
            ws = SimpleNamespace(
                id=f"t1-ws-{pipeline_run_id[:8]}",
                pipeline_run_id=pipeline_run_id,
                volume_name=f"lazyaf-ws-{pipeline_run_id[:8]}",
                status="ready",
                use_count=0,
            )
            self._workspaces[pipeline_run_id] = ws
        return ws

    async def acquire(self, db, workspace_id):
        self.ops.append(("acquire", workspace_id))

    async def release(self, db, workspace_id):
        self.ops.append(("release", workspace_id))

    async def cleanup(self, db, pipeline_run_id):
        self.ops.append(("cleanup", pipeline_run_id))
        self._workspaces.pop(pipeline_run_id, None)


@pytest.fixture(autouse=True)
def _t1_docker_free_local_execution(request):
    """Make the GLOBAL pipeline executor's LOCAL path Docker-free (T1).

    API-tier tests drive the app's global ``pipeline_executor``. Since Phase
    12.2-INT script/docker steps route LOCAL, and since Phase 12.4 the legacy
    runner queue REJECTS them outright - so forcing them legacy here (what
    this fixture used to do) would encode a production state that can no
    longer exist, and now trips the executor's enqueue guard.

    Instead we keep the REAL ExecutionRouter - script/docker route local,
    agent steps still route legacy until 12.5, exactly as in production - and
    swap only the two collaborators that would reach for Docker: the
    LocalExecutor and the WorkspaceService. T1 stays Docker-free while the
    routing decision under test stays the production one.

    Tests that exercise REAL local execution opt out with
    @pytest.mark.local_exec; unit tests that inject their own router/executor
    onto their OWN PipelineExecutor instances are untouched by design.
    """
    if request.node.get_closest_marker("local_exec"):
        yield
        return

    from app.services.pipeline_executor import pipeline_executor
    from app.services.workspace.execution_router import ExecutionRouter

    previous = (
        pipeline_executor._router,
        pipeline_executor._local_executor,
        pipeline_executor._workspace_service,
    )
    pipeline_executor._router = ExecutionRouter()
    pipeline_executor._local_executor = _T1StubLocalExecutor()
    pipeline_executor._workspace_service = _T1StubWorkspaceService()
    try:
        yield
    finally:
        (
            pipeline_executor._router,
            pipeline_executor._local_executor,
            pipeline_executor._workspace_service,
        ) = previous


@pytest_asyncio.fixture(autouse=True)
async def _drain_pipeline_executor():
    """After EVERY test, drain and reset the global pipeline executor.

    Uses the safe teardown from the executor itself (kill containers ->
    bounded grace -> cancel stragglers as a last resort - never a
    hard-cancel-mid-commit as the first move), so no asyncio task, state
    machine, run lock, or session factory leaks into the next test
    (12.2-INT fix 13). Cheap no-op when the executor was untouched.
    """
    yield
    from app.services.pipeline_executor import pipeline_executor

    if (
        pipeline_executor._tasks
        or pipeline_executor._state_machines
        or pipeline_executor._run_locks
        or pipeline_executor._session_factories
    ):
        await pipeline_executor.reset()
    elif pipeline_executor._local_executor is not None:
        pipeline_executor._local_executor.reset()


# -----------------------------------------------------------------------------
# Database Fixtures
# -----------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def async_engine(tmp_path):
    """Create a test database engine.

    FILE-backed, not ``:memory:``. Since script/docker steps route LOCAL,
    starting a pipeline from an API test spawns a background step task that
    opens its OWN session on this engine (see
    ``PipelineExecutor._session_factory_for``). SQLAlchemy serves a
    ``:memory:`` SQLite engine from a StaticPool - ONE shared DBAPI
    connection - so that task and the test's own ``db_session`` interleave
    commits and cursors on the same connection and raise
    ``sqlite3.InterfaceError: Cursor needed to be reset because of
    commit/rollback``. A file-backed engine gives each session a real
    connection, which is the same idiom
    ``tdd/unit/services/test_pipeline_local_dispatch.py`` already uses for
    this exact concurrency.
    """
    db_path = (tmp_path / "lazyaf_test.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        future=True,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    # Drain the global pipeline executor BEFORE the schema goes away. Since
    # script/docker steps route LOCAL, starting a pipeline spawns background
    # asyncio tasks that hold sessions bound to THIS engine; dropping the
    # tables underneath a still-running step task raises "no such table" out
    # of a task nobody is awaiting. Autouse fixtures tear down after the
    # fixtures they were set up before, so the drain has to happen here - at
    # the engine that owns the rows - not only in _drain_pipeline_executor.
    from app.services.pipeline_executor import pipeline_executor

    if (
        pipeline_executor._tasks
        or pipeline_executor._state_machines
        or pipeline_executor._run_locks
        or pipeline_executor._session_factories
    ):
        await pipeline_executor.reset()
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

    Uses the pool's own reset()/stop() (the same hooks the test-mode API
    uses) instead of hand-assigning private attributes, so the fixture
    cannot drift from the pool's real internals.
    """
    from app.services.runner_pool import runner_pool

    # Clear before
    if runner_pool._running:
        await runner_pool.stop()
    runner_pool.reset()

    yield runner_pool

    # Clear after
    if runner_pool._running:
        await runner_pool.stop()
    runner_pool.reset()


@pytest_asyncio.fixture
async def clean_job_queue():
    """Clean job queue state before and after each test.

    Uses the queue's own clear() (the same hook the test-mode API uses)
    instead of hand-assigning private attributes.
    """
    from app.services.job_queue import job_queue

    # Clear before
    await job_queue.clear()

    yield job_queue

    # Clear after
    await job_queue.clear()


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
