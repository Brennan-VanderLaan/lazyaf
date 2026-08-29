"""
Integration tests for the alembic migration path (Phase 0b).

Covers the startup scenarios init_db must handle:
- fresh empty database: upgrade-to-head builds the full schema, in parity
  with Base.metadata.create_all (which the test fixtures still use)
- legacy pre-alembic database (create_all-built, no alembic_version table):
  healed if a whole table is missing, parity-checked, then stamped at the
  baseline revision and upgraded, data intact
- drifted legacy database (a baseline table missing a column): startup
  refuses with a clear error instead of stamping over the drift
- database versioned by an unknown migration chain (real dev DBs carry the
  abandoned failure_01 branch's orphaned revision ids): startup refuses
  with a clear error instead of silently re-stamping
- repeated startup: a no-op
"""
import logging

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (register all tables on Base.metadata)
from app.database import ALEMBIC_BASELINE_REVISION, Base, _run_migrations

EXPECTED_TABLES = {
    "repos",
    "cards",
    "jobs",
    "runners",
    "agent_files",
    "pipelines",
    "pipeline_runs",
    "step_runs",
    "step_executions",
}


@pytest_asyncio.fixture
async def engine_factory(tmp_path):
    """Create file-backed async engines that are disposed after the test.

    File-backed (not :memory:) because the migration path is exercised across
    multiple connections, and because legacy dev DBs are files.
    """
    engines = []

    def factory(name: str):
        url = f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"
        engine = create_async_engine(url, echo=False)
        engines.append(engine)
        return engine

    yield factory

    for engine in engines:
        await engine.dispose()


async def _migrate(engine):
    """Run the startup migration path (what init_db does) against an engine."""
    async with engine.begin() as conn:
        await conn.run_sync(_run_migrations)


def _schema_snapshot(sync_conn):
    inspector = inspect(sync_conn)
    snapshot = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        columns = {
            col["name"]: (str(col["type"]), bool(col["nullable"]), bool(col["primary_key"]))
            for col in inspector.get_columns(table)
        }
        indexes = {
            idx["name"]: (tuple(idx["column_names"]), bool(idx["unique"]))
            for idx in inspector.get_indexes(table)
        }
        snapshot[table] = {"columns": columns, "indexes": indexes}
    return snapshot


async def _snapshot(engine):
    async with engine.connect() as conn:
        return await conn.run_sync(_schema_snapshot)


async def _table_names(engine):
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def _alembic_versions(engine):
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return [row[0] for row in result]


async def _create_all(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class TestFreshDatabase:
    async def test_upgrade_builds_full_schema(self, engine_factory):
        engine = engine_factory("fresh.db")
        await _migrate(engine)

        snapshot = await _snapshot(engine)
        assert set(snapshot) == EXPECTED_TABLES

    async def test_upgrade_matches_create_all_schema(self, engine_factory):
        """The migrated schema must be column-for-column what create_all builds."""
        migrated = engine_factory("migrated.db")
        created = engine_factory("created.db")

        await _migrate(migrated)
        await _create_all(created)

        migrated_schema = await _snapshot(migrated)
        created_schema = await _snapshot(created)

        assert set(migrated_schema) == set(created_schema)
        for table in created_schema:
            assert migrated_schema[table]["columns"] == created_schema[table]["columns"], table
            assert migrated_schema[table]["indexes"] == created_schema[table]["indexes"], table

    async def test_fresh_db_is_versioned_at_head(self, engine_factory):
        engine = engine_factory("fresh.db")
        await _migrate(engine)

        assert await _alembic_versions(engine) == [ALEMBIC_BASELINE_REVISION]


class TestLegacyDatabase:
    async def test_unstamped_db_gets_stamped_at_baseline(self, engine_factory):
        """A pre-alembic DB (create_all-built) is stamped, not re-migrated."""
        engine = engine_factory("legacy.db")
        await _create_all(engine)
        before = await _snapshot(engine)

        # Would raise "table repos already exists" if it re-ran the baseline
        await _migrate(engine)

        assert await _alembic_versions(engine) == [ALEMBIC_BASELINE_REVISION]
        assert await _snapshot(engine) == before

    async def test_legacy_data_survives_adoption(self, engine_factory):
        engine = engine_factory("legacy.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO repos (id, name, default_branch, is_ingested, created_at) "
                    "VALUES ('r1', 'keepme', 'main', 0, '2026-01-01 00:00:00')"
                )
            )

        await _migrate(engine)

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT name FROM repos WHERE id = 'r1'"))
            assert result.scalar_one() == "keepme"

    async def test_missing_table_is_healed_then_stamped(self, engine_factory):
        """A wholly-missing table is recreated by create_all before stamping."""
        engine = engine_factory("missing_table.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE step_executions"))

        await _migrate(engine)

        assert "step_executions" in await _table_names(engine)
        assert await _alembic_versions(engine) == [ALEMBIC_BASELINE_REVISION]

    async def test_missing_column_raises_drift_error_and_never_stamps(self, engine_factory):
        """A genuinely-behind DB (baseline table missing a column) is refused.

        Stamping it would record baseline parity that does not exist; the
        error must name the drift and the DB must stay unstamped.
        """
        engine = engine_factory("drifted.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE repos DROP COLUMN is_ingested"))

        with pytest.raises(RuntimeError, match=r"repos\.is_ingested") as excinfo:
            await _migrate(engine)

        assert "lazyaf-data" in str(excinfo.value)  # points at the remedy
        assert "alembic_version" not in await _table_names(engine)


class TestUnknownRevisionDatabase:
    async def test_unknown_revision_raises_and_is_not_restamped(self, engine_factory):
        """A dev DB versioned by failure_01's discarded chain fails loudly."""
        engine = engine_factory("orphaned.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            # revision id from failure_01's deleted version files
            await conn.execute(
                text("INSERT INTO alembic_version VALUES ('9495b26aec48')")
            )

        with pytest.raises(RuntimeError, match="9495b26aec48"):
            await _migrate(engine)

        # never silently re-stamped; the orphaned revision is left untouched
        assert await _alembic_versions(engine) == ["9495b26aec48"]

    async def test_unknown_revision_leaves_data_intact(self, engine_factory):
        engine = engine_factory("orphaned.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            await conn.execute(text("INSERT INTO alembic_version VALUES ('deadbeef1234')"))
            await conn.execute(
                text(
                    "INSERT INTO repos (id, name, default_branch, is_ingested, created_at) "
                    "VALUES ('r1', 'keepme', 'main', 0, '2026-01-01 00:00:00')"
                )
            )

        with pytest.raises(RuntimeError, match="deadbeef1234"):
            await _migrate(engine)

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT name FROM repos WHERE id = 'r1'"))
            assert result.scalar_one() == "keepme"

    async def test_empty_version_table_is_stamped(self, engine_factory):
        """An interrupted stamp leaves alembic_version empty; recover by adopting
        the DB as unversioned (heal + parity check + stamp)."""
        engine = engine_factory("empty_version.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )

        await _migrate(engine)

        assert await _alembic_versions(engine) == [ALEMBIC_BASELINE_REVISION]


class TestIdempotency:
    async def test_second_run_is_a_noop(self, engine_factory):
        engine = engine_factory("twice.db")
        await _migrate(engine)
        first = await _snapshot(engine)

        await _migrate(engine)

        assert await _snapshot(engine) == first
        assert await _alembic_versions(engine) == [ALEMBIC_BASELINE_REVISION]

    async def test_second_run_on_adopted_legacy_db(self, engine_factory):
        """Stamp on first startup, then plain no-op upgrades from then on."""
        engine = engine_factory("legacy_twice.db")
        await _create_all(engine)

        await _migrate(engine)
        await _migrate(engine)

        assert await _alembic_versions(engine) == [ALEMBIC_BASELINE_REVISION]


class TestLoggingIsUntouched:
    async def test_migrations_do_not_reconfigure_root_logger(self, engine_factory):
        """Programmatic runs skip alembic.ini's fileConfig: the app's root
        logger keeps its level and handlers (both fresh-DB upgrade and
        legacy-DB stamp paths run env.py)."""
        root = logging.getLogger()
        level_before = root.level
        handlers_before = list(root.handlers)

        fresh = engine_factory("log_fresh.db")
        await _migrate(fresh)

        legacy = engine_factory("log_legacy.db")
        await _create_all(legacy)
        await _migrate(legacy)

        assert root.level == level_before
        assert root.handlers == handlers_before
