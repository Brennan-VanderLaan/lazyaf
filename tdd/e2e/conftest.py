"""
E2E Test Fixtures - Full stack testing with Playwright and real backend.

Two modes of operation:
1. Quick mode (default): Uses ASGI test client, no real server
2. Full E2E mode: Starts real backend server + mock runner

Usage:
    # Quick tests (API only, no runner needed)
    pytest tdd/e2e/ -v -m "not slow"

    # Full E2E tests (requires mock runner)
    pytest tdd/e2e/ -v -m "slow"

Requirements for full mode:
    pip install pytest-playwright playwright
    playwright install chromium
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
import httpx

try:
    import websockets
except ImportError:
    websockets = None  # WebSocket tests will be skipped if not installed

# Add backend to path - handle both local and Docker environments
backend_path = Path(__file__).parent.parent.parent / "backend"
if not backend_path.exists():
    # Running inside Docker container where backend is at /app
    backend_path = Path("/app")
if backend_path.exists():
    sys.path.insert(0, str(backend_path))

# Add tdd root to path for conftest imports
tdd_path = Path(__file__).parent.parent
sys.path.insert(0, str(tdd_path))


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Allow override via environment for Docker-based testing
BACKEND_HOST = os.environ.get("E2E_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("E2E_BACKEND_PORT", "8765"))
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", f"http://{BACKEND_HOST}:{BACKEND_PORT}")
FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")

# Timeouts
BACKEND_STARTUP_TIMEOUT = 30  # seconds
JOB_COMPLETION_TIMEOUT = 60  # seconds
WEBSOCKET_TIMEOUT = 10  # seconds


# -----------------------------------------------------------------------------
# Backend Server Fixture
# -----------------------------------------------------------------------------

def wait_for_backend(url: str, timeout: int = BACKEND_STARTUP_TIMEOUT) -> bool:
    """Wait for backend to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"{url}/health", timeout=2)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def e2e_backend() -> Generator[str, None, None]:
    """Start a real backend server for E2E tests.

    This starts uvicorn in a subprocess with a fresh test database.
    The server is shared across all E2E tests in the session.

    Yields:
        str: The backend URL
    """
    # Create temp directory for test database
    temp_dir = tempfile.mkdtemp(prefix="lazyaf_e2e_")
    db_path = Path(temp_dir) / "test.db"
    git_repos_path = Path(temp_dir) / "git_repos"
    git_repos_path.mkdir()

    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    env["GIT_REPOS_DIR"] = str(git_repos_path)

    # Start uvicorn
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "app.main:app",
            "--host", BACKEND_HOST,
            "--port", str(BACKEND_PORT),
            "--log-level", "warning",
        ],
        cwd=str(backend_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Wait for backend to be ready
        if not wait_for_backend(BACKEND_URL):
            stdout, stderr = process.communicate(timeout=5)
            raise RuntimeError(
                f"Backend failed to start:\n"
                f"stdout: {stdout.decode()}\n"
                f"stderr: {stderr.decode()}"
            )

        yield BACKEND_URL

    finally:
        # Shutdown gracefully
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

        # Cleanup temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)


# -----------------------------------------------------------------------------
# HTTP Client Fixture
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def api_client(client) -> AsyncGenerator:
    """Async HTTP client for API calls.

    When E2E_BACKEND_URL is set (Docker mode): uses real HTTP client to connect
    to the running backend server, sharing the job queue with mock runners.

    Otherwise (quick tests): uses ASGI test client from root conftest for speed.
    """
    # Check if we're running in Docker mode (real backend server)
    if os.environ.get("E2E_BACKEND_URL"):
        # Use real HTTP client to connect to the running backend
        async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30.0) as real_client:
            yield real_client
    else:
        # Use ASGI test client for quick in-process tests
        yield client


@pytest_asyncio.fixture(autouse=True)
async def clear_job_queue_before_test(api_client):
    """Clear the job queue before each test to prevent job accumulation."""
    try:
        response = await api_client.post("/api/runners/clear-queue")
        if response.status_code == 200:
            data = response.json()
            if data.get("cleared", 0) > 0:
                import logging
                logging.info(f"Cleared {data['cleared']} jobs from queue before test")
    except Exception:
        # Ignore errors if endpoint doesn't exist or backend not ready
        pass
    yield


# -----------------------------------------------------------------------------
# WebSocket Client Fixture
# -----------------------------------------------------------------------------

class WebSocketTestClient:
    """WebSocket client for testing real-time events."""

    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.events: list[dict] = []
        self._receive_task = None

    async def connect(self):
        """Connect to WebSocket."""
        if websockets is None:
            raise RuntimeError("websockets package not installed")
        ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws = await websockets.connect(f"{ws_url}/ws")
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self):
        """Background task to receive events."""
        try:
            async for message in self.ws:
                try:
                    event = json.loads(message)
                    self.events.append(event)
                except json.JSONDecodeError:
                    pass
        except Exception:
            # Handle websockets.ConnectionClosed and other connection errors
            pass

    async def wait_for_event(
        self,
        event_type: str,
        timeout: float = WEBSOCKET_TIMEOUT,
        predicate: callable = None,
    ) -> dict | None:
        """Wait for a specific event type.

        Args:
            event_type: The event type to wait for
            timeout: Maximum time to wait
            predicate: Optional function to filter events

        Returns:
            The matching event or None if timeout
        """
        start = time.time()
        while time.time() - start < timeout:
            for event in self.events:
                if event.get("type") == event_type:
                    if predicate is None or predicate(event):
                        return event
            await asyncio.sleep(0.1)
        return None

    async def clear_events(self):
        """Clear received events."""
        self.events.clear()

    async def close(self):
        """Close connection."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()


@pytest_asyncio.fixture
async def websocket_client(e2e_backend: str) -> AsyncGenerator[WebSocketTestClient, None]:
    """WebSocket client for real-time event testing."""
    if websockets is None:
        pytest.skip("websockets package not installed")
    client = WebSocketTestClient(e2e_backend)
    await client.connect()
    yield client
    await client.close()


# -----------------------------------------------------------------------------
# Test Repo Fixtures
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def test_repo(api_client, clean_git_repos) -> dict:
    """Create and ingest a minimal test repository.

    Uses api_client fixture which connects to real backend in Docker mode.
    """
    import tempfile

    # Create temp git repo
    temp_dir = tempfile.mkdtemp(prefix="lazyaf_e2e_repo_")
    repo_path = Path(temp_dir)

    try:
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@lazyaf.local"],
            cwd=repo_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "LazyAF Test"],
            cwd=repo_path, check=True, capture_output=True
        )

        # Create initial files
        (repo_path / "README.md").write_text("# Test Repository\n\nFor E2E testing.\n")
        (repo_path / "src").mkdir()
        (repo_path / "src" / "main.py").write_text('print("Hello from test repo")\n')

        # Commit
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path, check=True, capture_output=True
        )

        # Ingest into LazyAF
        response = await api_client.post(
            "/api/repos/ingest",
            json={"path": str(repo_path), "name": "e2e-test-repo"},
        )
        assert response.status_code == 201, f"Failed to ingest repo: {response.text}"
        ingest_data = response.json()

        # Get full repo data
        repo_response = await api_client.get(f"/api/repos/{ingest_data['id']}")
        assert repo_response.status_code == 200

        yield repo_response.json()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# -----------------------------------------------------------------------------
# Mock Executor Config Helpers
# -----------------------------------------------------------------------------

def mock_config_simple_change(
    file_path: str = "src/new_feature.py",
    content: str = "# Auto-generated by mock executor\ndef new_feature():\n    pass\n",
    message: str = "Implementing feature...",
) -> dict:
    """Create a simple mock config that adds one file."""
    return {
        "response_mode": "streaming",
        "delay_ms": 50,
        "file_operations": [
            {"action": "create", "path": file_path, "content": content}
        ],
        "output_events": [
            {"type": "content", "text": "Analyzing codebase..."},
            {"type": "tool_use", "tool": "Read", "path": "README.md"},
            {"type": "content", "text": message},
            {"type": "tool_use", "tool": "Write", "path": file_path},
            {"type": "complete", "text": "Implementation complete."}
        ],
        "exit_code": 0
    }


def mock_config_modify_file(
    file_path: str = "src/main.py",
    search: str = 'print("Hello',
    replace: str = 'print("Modified by mock executor: Hello',
) -> dict:
    """Create a mock config that modifies an existing file."""
    return {
        "response_mode": "streaming",
        "delay_ms": 50,
        "file_operations": [
            {"action": "modify", "path": file_path, "search": search, "replace": replace}
        ],
        "output_events": [
            {"type": "content", "text": "Reading existing code..."},
            {"type": "tool_use", "tool": "Read", "path": file_path},
            {"type": "content", "text": "Making modifications..."},
            {"type": "tool_use", "tool": "Edit", "path": file_path},
            {"type": "complete", "text": "Modifications complete."}
        ],
        "exit_code": 0
    }


def mock_config_error(error_message: str = "Mock executor error") -> dict:
    """Create a mock config that fails."""
    return {
        "response_mode": "batch",
        "delay_ms": 50,
        "file_operations": [],
        "output_events": [
            {"type": "content", "text": "Starting analysis..."},
            {"type": "error", "text": error_message}
        ],
        "exit_code": 1,
        "error_message": error_message
    }


@pytest.fixture
def mock_config():
    """Factory fixture for creating mock executor configs."""
    return {
        "simple_change": mock_config_simple_change,
        "modify_file": mock_config_modify_file,
        "error": mock_config_error,
    }


# -----------------------------------------------------------------------------
# Card Helpers
# -----------------------------------------------------------------------------

async def create_card_with_mock(
    api_client: httpx.AsyncClient,
    repo_id: str,
    title: str = "Test Card",
    description: str = "Test card for E2E testing",
    mock_config: dict = None,
) -> dict:
    """Create a card configured to use the mock executor.

    Args:
        api_client: The HTTP client
        repo_id: Repository ID
        title: Card title
        description: Card description
        mock_config: Mock executor configuration

    Returns:
        dict: The created card data
    """
    step_config = {}
    if mock_config:
        step_config["mock_config"] = mock_config

    response = await api_client.post(
        f"/api/repos/{repo_id}/cards",
        json={
            "title": title,
            "description": description,
            "runner_type": "mock",
            "step_type": "agent",
            "step_config": step_config,
        },
    )
    assert response.status_code == 201, f"Failed to create card: {response.text}"
    return response.json()


async def start_card(api_client: httpx.AsyncClient, card_id: str) -> dict:
    """Start work on a card.

    Args:
        api_client: The HTTP client
        card_id: Card ID to start

    Returns:
        dict: The response data
    """
    response = await api_client.post(f"/api/cards/{card_id}/start")
    assert response.status_code == 200, f"Failed to start card: {response.text}"
    return response.json()


async def wait_for_card_status(
    api_client: httpx.AsyncClient,
    card_id: str,
    expected_status: str,
    timeout: float = JOB_COMPLETION_TIMEOUT,
) -> dict:
    """Wait for a card to reach a specific status.

    Args:
        api_client: The HTTP client
        card_id: Card ID
        expected_status: Status to wait for (e.g., "in_review", "failed")
        timeout: Maximum time to wait

    Returns:
        dict: The card data when it reaches the expected status

    Raises:
        TimeoutError: If the card doesn't reach the status in time
    """
    start = time.time()
    while time.time() - start < timeout:
        response = await api_client.get(f"/api/cards/{card_id}")
        if response.status_code == 200:
            card = response.json()
            if card["status"] == expected_status:
                return card
            # Check for terminal states
            if card["status"] in ("done", "failed") and card["status"] != expected_status:
                raise AssertionError(
                    f"Card reached terminal status {card['status']} "
                    f"instead of expected {expected_status}"
                )
        await asyncio.sleep(0.5)

    raise TimeoutError(f"Card {card_id} did not reach status {expected_status} within {timeout}s")


@pytest.fixture
def card_helpers(api_client):
    """Card helper functions fixture."""
    return {
        "create_with_mock": lambda **kwargs: create_card_with_mock(api_client, **kwargs),
        "start": lambda card_id: start_card(api_client, card_id),
        "wait_for_status": lambda card_id, status, **kwargs: wait_for_card_status(
            api_client, card_id, status, **kwargs
        ),
    }


# -----------------------------------------------------------------------------
# Playwright Browser Fixtures (optional - only if playwright is installed)
# -----------------------------------------------------------------------------

try:
    from playwright.async_api import async_playwright, Page, Browser

    PLAYWRIGHT_AVAILABLE = True

    @pytest_asyncio.fixture(scope="session")
    async def browser() -> AsyncGenerator[Browser, None]:
        """Playwright browser instance."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            yield browser
            await browser.close()

    @pytest_asyncio.fixture
    async def page(browser: Browser, e2e_backend: str) -> AsyncGenerator[Page, None]:
        """Fresh browser page for each test."""
        context = await browser.new_context()
        page = await context.new_page()

        # Navigate to frontend (assumes it's running)
        # In CI, you might need to start the frontend too
        try:
            await page.goto(FRONTEND_URL, timeout=5000)
        except Exception:
            # Frontend not running - tests will need to use API only
            pass

        yield page
        await context.close()

except ImportError:
    PLAYWRIGHT_AVAILABLE = False

    @pytest.fixture
    def browser():
        pytest.skip("Playwright not installed. Run: pip install pytest-playwright && playwright install")

    @pytest.fixture
    def page():
        pytest.skip("Playwright not installed. Run: pip install pytest-playwright && playwright install")


# -----------------------------------------------------------------------------
# Test Markers
# -----------------------------------------------------------------------------

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "e2e: End-to-end tests")
    config.addinivalue_line("markers", "slow: Tests that take a long time")
    config.addinivalue_line("markers", "browser: Tests that require Playwright browser")
