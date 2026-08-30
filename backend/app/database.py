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

engine = create_async_engine(settings.database_url, echo=settings.db_echo)
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


def _baseline_columns() -> dict[str, set[str]]:
    """Table -> column names exactly as revision 0001 defines them.

    Derived by running the REAL baseline migration against a scratch
    in-memory database (no hand-maintained copy that could drift from the
    migration files).
    """
    scratch = sa.create_engine("sqlite://")
    try:
        with scratch.connect() as conn:
            command.upgrade(_alembic_config(conn), ALEMBIC_BASELINE_REVISION)
            inspector = sa.inspect(conn)
            return {
                name: {col["name"] for col in inspector.get_columns(name)}
                for name in inspector.get_table_names()
                if name != "alembic_version"
            }
    finally:
        scratch.dispose()


def _adopt_unversioned(config: AlembicConfig, connection: sa.Connection) -> None:
    """Adopt an unversioned database: heal, classify, then stamp.

    create_all first restores wholly-missing tables at the CURRENT model
    schema (the old pre-alembic startup behavior; it never touches existing
    tables). The healed schema is then classified three ways:

    1. Matches the current model metadata column-for-column -> the DB is
       already head-shaped (create_all-built by the pre-alembic startup, or
       fully hand-migrated): stamp HEAD. Stamping the baseline instead
       would re-run 0002/0003 over objects that already exist and record a
       lineage the schema never had.
    2. Matches revision 0001's columns -> a genuinely old pre-alembic DB:
       stamp the BASELINE and let the caller's upgrade-to-head run 0002+
       to add the missing columns/tables (create_all cannot add COLUMNS to
       existing tables, only whole tables).
    3. Anything else is drift -> refuse loudly. Stamping would record
       parity that does not exist and every later migration would build on
       the lie.

    This function OWNS schema-drift detection for adopted databases; the
    later migrations' skip-if-present guards rely on it.
    """
    import app.models  # noqa: F401  (register all tables on Base.metadata)

    Base.metadata.create_all(connection)

    inspector = sa.inspect(connection)

    def missing_from(spec: dict[str, set[str]]) -> list[str]:
        out: list[str] = []
        for table, columns in spec.items():
            actual = {col["name"] for col in inspector.get_columns(table)}
            out.extend(f"{table}.{c}" for c in sorted(columns) if c not in actual)
        return out

    current = {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
    }
    missing_current = missing_from(current)
    if not missing_current:
        command.stamp(config, "head")
        return

    missing_baseline = missing_from(_baseline_columns())
    if not missing_baseline:
        command.stamp(config, ALEMBIC_BASELINE_REVISION)
        return

    raise RuntimeError(
        "Refusing to adopt drifted database: missing column(s) "
        f"{', '.join(sorted(missing_baseline))} (vs the 0001 baseline; vs the "
        f"current models: {', '.join(sorted(missing_current))}). {_RECREATE_HINT}"
    )


def _run_migrations(connection: sa.Connection) -> None:
    """Bring the schema to alembic head over an existing sync connection.

    A database created before alembic existed (by create_all + the old
    ALTER hacks) is healed and adopted (stamped at head or at the baseline
    depending on its shape — see _adopt_unversioned); one versioned by an
    unknown chain (e.g. the abandoned failure_01 branch) fails loudly;
    fresh databases are built entirely by upgrade.
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
