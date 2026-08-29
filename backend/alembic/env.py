"""Alembic migration environment for the LazyAF backend."""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Add backend/ to path so `app` imports resolve regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base  # noqa: E402

# Import all models so every table is registered on Base.metadata
import app.models  # noqa: F401, E402

config = context.config

# Configure logging for CLI invocations only. Programmatic runs (app startup
# via database.py) set configure_logger=False: fileConfig would clobber the
# application's already-configured root logger. disable_existing_loggers=False
# keeps module-level loggers alive even for CLI runs.
if config.config_file_name is not None and config.attributes.get("configure_logger", True) is not False:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve a sync-driver URL for CLI/offline runs.

    Programmatic callers (app startup, tests) bypass this by passing a live
    connection via config.attributes["connection"].
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        from app.config import get_settings

        url = get_settings().database_url
    # Alembic runs sync; strip the async sqlite driver
    return url.replace("sqlite+aiosqlite", "sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_with(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # required for SQLite ALTER TABLE support
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection.

    Uses the connection provided by a programmatic caller when present
    (startup runs alembic through the app's async engine via run_sync),
    otherwise builds a sync engine from settings for CLI use.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations_with(connection)
        return

    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run_migrations_with(conn)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
