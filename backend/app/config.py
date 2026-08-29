from pydantic import BaseModel
from functools import lru_cache
import os


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
    )
