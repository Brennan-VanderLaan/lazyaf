from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Revision pre-alembic databases are stamped at (see 0001_baseline.py).
ALEMBIC_BASELINE_REVISION = "0001"

_ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"

_RECREATE_HINT = (
    "Recreate the dev database (docker compose down && docker volume rm lazyaf-data) "
    "or migrate it manually."
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


def _alembic_config(connection: sa.Connection) -> AlembicConfig:
    config = AlembicConfig(str(_ALEMBIC_INI_PATH))
    config.attributes["connection"] = connection
    # Programmatic run: env.py must not reconfigure the app's logging.
    config.attributes["configure_logger"] = False
    return config


def _unknown_revisions(config: AlembicConfig, versions: list[str]) -> list[str]:
    """Revisions in alembic_version that are absent from our migration chain."""
    script = ScriptDirectory.from_config(config)
    unknown = []
    for revision in versions:
        try:
            script.get_revision(revision)
        except CommandError:  # ScriptDirectory wraps ResolutionError
            unknown.append(revision)
    return unknown


def _adopt_unversioned(config: AlembicConfig, connection: sa.Connection) -> None:
    """Adopt a pre-alembic database: heal, verify parity, then stamp.

    create_all restores wholly-missing tables (the old pre-alembic startup
    behavior; it never touches existing ones). Existing tables are then
    checked column-for-column against the model metadata: a drifted schema
    is refused loudly, because stamping it would record baseline parity that
    does not exist and every later migration would build on the lie.
    """
    import app.models  # noqa: F401  (register all tables on Base.metadata)

    Base.metadata.create_all(connection)

    inspector = sa.inspect(connection)
    drifted = []
    for table in Base.metadata.sorted_tables:
        actual = {col["name"] for col in inspector.get_columns(table.name)}
        drifted.extend(
            f"{table.name}.{column.name}"
            for column in table.columns
            if column.name not in actual
        )
    if drifted:
        raise RuntimeError(
            "Refusing to adopt drifted database: missing column(s) "
            f"{', '.join(sorted(drifted))}. {_RECREATE_HINT}"
        )

    command.stamp(config, ALEMBIC_BASELINE_REVISION)


def _run_migrations(connection: sa.Connection) -> None:
    """Bring the schema to alembic head over an existing sync connection.

    A database created before alembic existed (by create_all + the old
    ALTER hacks) is healed, parity-checked, and stamped at the baseline
    revision rather than migrated; one versioned by an unknown chain (e.g.
    the abandoned failure_01 branch) fails loudly; fresh databases are
    built entirely by upgrade.
    """
    config = _alembic_config(connection)

    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    has_app_tables = "repos" in tables

    if has_app_tables:
        if "alembic_version" not in tables:
            _adopt_unversioned(config, connection)
        else:
            versions = (
                connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
            unknown = _unknown_revisions(config, list(versions))
            if unknown:
                raise RuntimeError(
                    "alembic_version holds unknown revision(s) "
                    f"{', '.join(unknown)}: this database was versioned by a "
                    f"migration chain this codebase does not have. {_RECREATE_HINT}"
                )
            if not versions:
                # An interrupted stamp leaves alembic_version empty; adopt the
                # database as unversioned (heal + parity check + stamp).
                _adopt_unversioned(config, connection)

    command.upgrade(config, "head")


async def init_db() -> None:
    """Bring the database to alembic head. Idempotent; runs at startup."""
    async with engine.begin() as conn:
        await conn.run_sync(_run_migrations)
