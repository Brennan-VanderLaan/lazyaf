from pydantic import BaseModel
from functools import lru_cache
import json
import logging
import os

logger = logging.getLogger(__name__)


def _parse_gpu_node_rates(raw: str | None) -> dict:
    """Parse LAZYAF_GPU_NODE_RATES (JSON object) — never fatal.

    A malformed rate table must not stop the backend from booting: it is
    priced-as-unknown telemetry configuration, not a correctness input. The
    parse failure is logged loudly and the table falls back to empty.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "LAZYAF_GPU_NODE_RATES is not valid JSON — gpu-node steps will be "
            "priced as cost_source='unknown'"
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "LAZYAF_GPU_NODE_RATES must be a JSON object keyed by node id, got "
            "%s — gpu-node steps will be priced as cost_source='unknown'",
            type(parsed).__name__,
        )
        return {}
    return parsed


class Settings(BaseModel):
    app_name: str = "LazyAF"
    database_url: str = "sqlite+aiosqlite:///./lazyaf.db"
    cors_origins: list[str] = ["http://localhost:5173"]
    docker_host: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    default_runner_type: str = "any"  # any, claude-code, gemini
    default_prompt_template: str | None = None  # Global default prompt template for AI agents
    # Mounts the /api/test reset/seed endpoints (e2e harness only - never prod)
    test_mode: bool = False
    # --- Execution plumbing (Phase 12.2-INT) ---
    # Named docker network shared by backend, runners, and step/helper containers.
    container_network: str = "lazyaf-network"
    # Clone URL template used by workspace population helper containers.
    # Resolved on the container network (backend service DNS name), not localhost.
    container_git_url_template: str = "http://backend:8000/git/{repo_id}.git"
    # Image for the short-lived workspace population (git clone) helper container.
    workspace_clone_image: str = "python:3.12"
    # Backend base URL as seen FROM step/helper containers on the container
    # network (contract #2: injected into step env as LAZYAF_BACKEND_URL).
    container_backend_url: str = "http://backend:8000"
    # Default image for pipeline steps (moved here from LocalExecutor).
    # Full python image (not slim) until 12.3's lazyaf-base: bash/curl/git needed.
    step_default_image: str = "python:3.12"
    # Default working directory for step containers.
    step_working_dir: str = "/workspace/repo"
    # HOME inside step containers - lives on the shared workspace volume so
    # tools installed in one step survive to the next (12.3 persistence contract).
    step_home_dir: str = "/workspace/home"
    # Secret for step auth tokens (control layer <-> /api/steps/*). Default is
    # the long-standing dev constant; override in real deployments.
    step_auth_secret: str = "lazyaf-step-auth-secret-key-change-in-production"
    # --- Usage channel (Phase 12.5) ---
    # Self-hosted node hourly rates, addressed by node id (api-surface 2.5):
    #   {"runpod-a100-80g": {"rate_usd_hour": "1.89", "note": "..."}}
    # The SERVER prices gpu-node steps from this table so a corrected rate can
    # re-price history. Empty by default: nothing sets LAZYAF_GPU_NODE_ID
    # until 12.6 puts steps on real nodes.
    gpu_node_rates: dict = {}

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./lazyaf.db"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        docker_host=os.getenv("DOCKER_HOST"),
        default_runner_type=os.getenv("DEFAULT_RUNNER_TYPE", "any"),
        default_prompt_template=os.getenv("DEFAULT_PROMPT_TEMPLATE"),
        test_mode=os.getenv("LAZYAF_TEST_MODE", "").lower() in ("1", "true", "yes"),
        container_network=os.getenv("CONTAINER_NETWORK", "lazyaf-network"),
        container_git_url_template=os.getenv(
            "CONTAINER_GIT_URL_TEMPLATE", "http://backend:8000/git/{repo_id}.git"
        ),
        workspace_clone_image=os.getenv("WORKSPACE_CLONE_IMAGE", "python:3.12"),
        container_backend_url=os.getenv("CONTAINER_BACKEND_URL", "http://backend:8000"),
        step_default_image=os.getenv("STEP_DEFAULT_IMAGE", "python:3.12"),
        step_working_dir=os.getenv("STEP_WORKING_DIR", "/workspace/repo"),
        step_home_dir=os.getenv("STEP_HOME_DIR", "/workspace/home"),
        step_auth_secret=os.getenv(
            "LAZYAF_STEP_AUTH_SECRET",
            "lazyaf-step-auth-secret-key-change-in-production",
        ),
        gpu_node_rates=_parse_gpu_node_rates(os.getenv("LAZYAF_GPU_NODE_RATES")),
    )
