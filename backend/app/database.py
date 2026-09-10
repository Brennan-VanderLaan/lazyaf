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

#: Columns a revision has DROPPED, mapped to THE REVISION TO STAMP when one is
#: found still present in an unversioned database.
#:
#: That revision is the PARENT OF THE FIRST revision in the column's
#: retirement, which is not always the parent of the drop. `steps` is the
#: worked example: 0014 backfills every array into `steps_graph` and 0015 drops
#: the column, and stamping 0014 would mark the backfill as already done - so
#: `upgrade("head")` would run 0015 alone, which then correctly REFUSES because
#: rows still hold an array and no graph. The whole retirement has to run, so
#: the entry names 0013.
#:
#: This exists because `_adopt_unversioned` classifies an unversioned database
#: by asking only what is MISSING - it walks the expected column set and checks
#: presence. A column present in the DATABASE but absent from the MODELS is
#: invisible to it, so a retirement inverts its logic: after 0015 a pre-alembic
#: dev database (the `lazyaf-data` docker volume, built by the old create_all)
#: has everything the models declare, `missing_current` is empty, and it is
#: stamped at HEAD. The backfill never runs, the drop never runs, and
#: `pipelines.steps` survives as an orphan - still NOT NULL with no server
#: default - so the next pipeline INSERT dies with
#: `NOT NULL constraint failed: pipelines.steps`, from a database that reports
#: itself as being at head. Silent, and it bricks writes rather than reads.
#:
#: The fix heals rather than refuses: stamp the named revision and let the
#: caller's `command.upgrade(config, "head")` run the retirement properly, so
#: the array is backfilled into a graph before the column goes. Re-running the
#: revisions in between costs nothing - every revision in this chain is guarded
#: by presence checks and is re-runnable, which is the same property the
#: baseline-stamp branch below has always relied on.
#:
#: MAINTENANCE: every future column drop adds an entry here, and a renumbered
#: revision moves its value. An entry whose revision does not exist in the
#: chain is caught by `_validate_retired_columns` below rather than being
#: discovered as a failed stamp at startup, and
#: `tdd/integration/test_migrations_pipeline_retirement.py` pins both the value
#: and the healing it is supposed to produce.
_RETIRED_COLUMNS: dict[tuple[str, str], str] = {
    # 12.8 P6: the v1 array pipeline format. 0014 backfills every array into
    # `steps_graph`, 0015 drops the column - so the stamp is 0013, the parent
    # of the backfill, and BOTH then run.
    ("pipelines", "steps"): "0013",
}


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


def _validate_retired_columns(config: AlembicConfig) -> None:
    """Every `_RETIRED_COLUMNS` value must name a revision the chain has.

    A typo here would be found by `command.stamp` raising at STARTUP, on the
    one database shape this mapping exists to rescue - i.e. only on the
    machine it was supposed to help. Checked up front instead, so the failure
    lands on whoever edited the mapping.
    """
    script = ScriptDirectory.from_config(config)
    for (table, column), revision in _RETIRED_COLUMNS.items():
        try:
            script.get_revision(revision)
        except CommandError as exc:  # ScriptDirectory wraps ResolutionError
            raise RuntimeError(
                f"_RETIRED_COLUMNS maps {table}.{column} to revision "
                f"{revision!r}, which is not in the migration chain. A "
                "renumbered revision must move its entry too."
            ) from exc


def _retired_columns_present(
    config: AlembicConfig, inspector: sa.Inspector, tables: set[str]
) -> str | None:
    """The revision to stamp when a dropped column is still in the database.

    Returns the EARLIEST such revision (chain order) when several retired
    columns are present, so one upgrade retires all of them; None when the
    database carries no retired column at all. See `_RETIRED_COLUMNS` for why
    this check has to exist: the classifier below asks what is missing, and a
    retired column is the opposite shape - present in the database, absent
    from the models - so nothing else can see it.
    """
    _validate_retired_columns(config)

    found: list[str] = []
    for (table, column), revision in _RETIRED_COLUMNS.items():
        if table not in tables:
            continue
        if column in {col["name"] for col in inspector.get_columns(table)}:
            found.append(revision)

    if not found:
        return None

    # Order by the chain, not by string comparison: revision ids are opaque
    # and only alembic knows which of two comes first. Walking down from head
    # and taking the LAST match is the earliest of them.
    script = ScriptDirectory.from_config(config)
    ordered = [rev.revision for rev in script.walk_revisions()]
    return sorted(set(found), key=ordered.index)[-1]


def _adopt_unversioned(config: AlembicConfig, connection: sa.Connection) -> None:
    """Adopt an unversioned database: heal, classify, then stamp.

    create_all first restores wholly-missing tables at the CURRENT model
    schema (the old pre-alembic startup behavior; it never touches existing
    tables). The healed schema is then classified four ways:

    0. Still carries a column a later revision DROPPED -> stamp the revision
       immediately BEFORE that drop and let the caller's upgrade-to-head run
       the retirement properly. This case is checked FIRST because it is
       invisible to the three below, which all classify by what is missing;
       a retired column is present-but-unmodelled, which reads to them as
       perfect parity. See `_RETIRED_COLUMNS`.
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

    pre_drop = _retired_columns_present(
        config, inspector, set(inspector.get_table_names())
    )
    if pre_drop is not None and not missing_current:
        # Head-shaped EXCEPT that a dropped column is still there. Stamping
        # head would strand it: `pipelines.steps` is NOT NULL with no server
        # default, so the first INSERT after that would fail on a database
        # reporting itself as current.
        command.stamp(config, pre_drop)
        return

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
