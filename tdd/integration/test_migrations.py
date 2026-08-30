"""
Integration tests for the alembic migration path (Phase 0b).

Covers the startup scenarios init_db must handle:
- fresh empty database: upgrade-to-head builds the full schema, in parity
  with Base.metadata.create_all (which the test fixtures still use)
- unversioned databases (no alembic_version table) are adopted three ways
  (_adopt_unversioned in app/database.py):
  1. head-shaped (matches the current models column-for-column, e.g. a
     create_all-built dev DB) -> stamped at HEAD, schema untouched
  2. baseline-shaped (columns exactly as revision 0001 defines, i.e. a
     genuinely old pre-alembic DB without the 0002/0003 columns) ->
     stamped at 0001, then upgraded so 0002/0003 add what's missing
  3. anything else (a baseline table missing a column) is drift ->
     startup refuses with a clear error instead of stamping over it
- database versioned by an unknown migration chain (real dev DBs carry the
  abandoned failure_01 branch's orphaned revision ids): startup refuses
  with a clear error instead of silently re-stamping
- repeated startup: a no-op
"""
import logging

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (register all tables on Base.metadata)
from app.database import ALEMBIC_BASELINE_REVISION, Base, _alembic_config, _run_migrations

# Tip of the migration chain. Every startup path (fresh upgrade, legacy
# adoption stamp-then-upgrade) must end here.
ALEMBIC_HEAD_REVISION = "0004"

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
    # 0002 (Phase 12.2-INT)
    "workspaces",
    # 0003 (Phase 12.2.5 spec layer)
    "features",
    "user_stories",
    "acceptance_criteria",
    "prompt_templates",
    # 0004 (Phase 12.2.6 test tie-back)
    "test_refs",
    "test_runs",
}

SPEC_TABLES = {"features", "user_stories", "acceptance_criteria", "prompt_templates"}

TEST_TIEBACK_TABLES = {"test_refs", "test_runs"}


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


async def _upgrade_to(engine, revision):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.upgrade(_alembic_config(c), revision))


async def _downgrade_to(engine, revision):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(_alembic_config(c), revision))


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

        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]


async def _make_baseline_shaped(engine):
    """Build a genuinely old pre-alembic DB: exactly the 0001 schema (via the
    real baseline migration's DDL), WITHOUT the 0002/0003 columns/tables and
    WITHOUT an alembic_version table."""
    await _upgrade_to(engine, ALEMBIC_BASELINE_REVISION)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE alembic_version"))


class TestLegacyDatabase:
    async def test_unstamped_headshaped_db_is_stamped_at_head(self, engine_factory):
        """Adoption branch 1: a create_all-built DB already matches the
        current models, so it is stamped at HEAD (stamping the baseline
        would re-run 0002/0003 over it) and the schema is untouched."""
        engine = engine_factory("legacy.db")
        await _create_all(engine)
        before = await _snapshot(engine)

        # Would raise "table repos already exists" if it re-ran the baseline
        await _migrate(engine)

        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]
        assert await _snapshot(engine) == before

    async def test_baseline_shaped_db_is_stamped_at_baseline_and_upgraded(
        self, engine_factory
    ):
        """Adoption branch 2: a truly old pre-alembic DB (0001 columns only)
        must NOT be stamped at head — cards.feature_id etc. do not exist and
        create_all cannot add columns to existing tables. It is stamped at
        0001 so the real 0002/0003 upgrades add the missing pieces."""
        engine = engine_factory("baseline_shaped.db")
        await _make_baseline_shaped(engine)
        before = await _snapshot(engine)
        assert "workspaces" not in before
        assert "feature_id" not in before["cards"]["columns"]
        assert "executor" not in before["step_runs"]["columns"]

        await _migrate(engine)

        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]
        after = await _snapshot(engine)
        assert set(after) == EXPECTED_TABLES
        assert "feature_id" in after["cards"]["columns"]
        assert "user_story_id" in after["cards"]["columns"]
        assert "executor" in after["step_runs"]["columns"]

    async def test_baseline_shaped_db_lands_in_full_head_parity(self, engine_factory):
        """The adopted-and-upgraded legacy DB is column-for-column and
        index-for-index identical to a freshly migrated one."""
        legacy = engine_factory("baseline_parity.db")
        fresh = engine_factory("fresh_parity.db")
        await _make_baseline_shaped(legacy)

        await _migrate(legacy)
        await _migrate(fresh)

        legacy_schema = await _snapshot(legacy)
        fresh_schema = await _snapshot(fresh)
        assert set(legacy_schema) == set(fresh_schema)
        for table in fresh_schema:
            assert legacy_schema[table]["columns"] == fresh_schema[table]["columns"], table
            assert legacy_schema[table]["indexes"] == fresh_schema[table]["indexes"], table

    async def test_baseline_shaped_db_data_survives(self, engine_factory):
        engine = engine_factory("baseline_data.db")
        await _make_baseline_shaped(engine)
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
            # The 0003 columns exist and read NULL for pre-existing rows.
            result = await conn.execute(
                text("SELECT feature_id FROM cards LIMIT 1")
            )  # must not raise "no such column"
            assert result.first() is None

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
        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]

    async def test_missing_column_raises_drift_error_and_never_stamps(self, engine_factory):
        """Adoption branch 3: a DB matching NEITHER the current models NOR
        the 0001 baseline (a baseline table missing a baseline column) is
        refused. Stamping it would record parity that does not exist; the
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

        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]


class TestIdempotency:
    async def test_second_run_is_a_noop(self, engine_factory):
        engine = engine_factory("twice.db")
        await _migrate(engine)
        first = await _snapshot(engine)

        await _migrate(engine)

        assert await _snapshot(engine) == first
        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]

    async def test_second_run_on_adopted_legacy_db(self, engine_factory):
        """Stamp on first startup, then plain no-op upgrades from then on."""
        engine = engine_factory("legacy_twice.db")
        await _create_all(engine)

        await _migrate(engine)
        await _migrate(engine)

        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]


class TestRoundTrip:
    """upgrade -> downgrade -> upgrade coverage for revisions 0002 and 0003."""

    async def test_downgrade_to_0003_removes_test_tieback_only(self, engine_factory):
        """0004's downgrade drops test_refs/test_runs and nothing else: the
        spec tables, workspaces and step_runs.executor all stay."""
        engine = engine_factory("rt_0003.db")
        await _migrate(engine)

        await _downgrade_to(engine, "0003")

        snapshot = await _snapshot(engine)
        assert TEST_TIEBACK_TABLES.isdisjoint(set(snapshot))
        assert SPEC_TABLES <= set(snapshot)
        assert "workspaces" in snapshot
        assert "executor" in snapshot["step_runs"]["columns"]
        assert await _alembic_versions(engine) == ["0003"]

    async def test_downgrade_to_0002_removes_spec_layer_only(self, engine_factory):
        """0003's downgrade drops the spec tables and the cards link columns,
        leaving 0002's objects (workspaces, step_runs.executor) intact.
        (0004's downgrade runs first on the way down, dropping the test
        tie-back tables.)"""
        engine = engine_factory("rt_0002.db")
        await _migrate(engine)

        await _downgrade_to(engine, "0002")

        snapshot = await _snapshot(engine)
        assert SPEC_TABLES.isdisjoint(set(snapshot))
        assert TEST_TIEBACK_TABLES.isdisjoint(set(snapshot))
        assert "feature_id" not in snapshot["cards"]["columns"]
        assert "user_story_id" not in snapshot["cards"]["columns"]
        assert "workspaces" in snapshot
        assert "executor" in snapshot["step_runs"]["columns"]
        assert await _alembic_versions(engine) == ["0002"]

    async def test_downgrade_to_baseline_matches_pure_0001_schema(self, engine_factory):
        """Downgrading head -> 0001 restores exactly the baseline schema
        (columns and indexes), byte-for-byte with a fresh 0001 upgrade."""
        engine = engine_factory("rt_down.db")
        reference = engine_factory("rt_ref.db")

        await _migrate(engine)
        await _downgrade_to(engine, "0001")
        await _upgrade_to(reference, "0001")

        assert await _snapshot(engine) == await _snapshot(reference)
        assert await _alembic_versions(engine) == [ALEMBIC_BASELINE_REVISION]

    async def test_roundtrip_restores_head_schema(self, engine_factory):
        """head -> 0001 -> head lands on the identical schema: 0002/0003's
        upgrades and downgrades are exact inverses."""
        engine = engine_factory("rt_full.db")
        await _migrate(engine)
        head = await _snapshot(engine)

        await _downgrade_to(engine, "0001")
        await _upgrade_to(engine, "head")

        assert await _snapshot(engine) == head
        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]

    async def test_roundtrip_preserves_rows_in_altered_tables(self, engine_factory):
        """cards and step_runs rows survive the column drops/re-adds; the
        dropped columns' values are gone by design and come back NULL."""
        engine = engine_factory("rt_data.db")
        await _migrate(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO repos (id, name, default_branch, is_ingested, created_at) "
                    "VALUES ('r1', 'repo', 'main', 0, '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO features (id, title, description, status, repo_ids, created_at, updated_at) "
                    "VALUES ('f1', 'feat', '', 'draft', '[]', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO cards (id, repo_id, title, description, status, runner_type, "
                    "step_type, feature_id, created_at, updated_at) "
                    "VALUES ('c1', 'r1', 'keepme', '', 'todo', 'any', 'agent', 'f1', "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, repo_id, name, steps, triggers, is_template, created_at, updated_at) "
                    "VALUES ('p1', 'r1', 'pipe', '[]', '[]', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_runs (id, pipeline_id, status, trigger_type, "
                    "current_step, steps_completed, steps_total, created_at) "
                    "VALUES ('pr1', 'p1', 'passed', 'manual', 0, 1, 1, '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO step_runs (id, pipeline_run_id, step_index, step_name, status, executor, logs) "
                    "VALUES ('sr1', 'pr1', 0, 'build', 'passed', 'local', '')"
                )
            )

        await _downgrade_to(engine, "0001")

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT title FROM cards WHERE id = 'c1'"))
            assert result.scalar_one() == "keepme"
            result = await conn.execute(text("SELECT step_name FROM step_runs WHERE id = 'sr1'"))
            assert result.scalar_one() == "build"

        await _upgrade_to(engine, "head")

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT title, feature_id, user_story_id FROM cards WHERE id = 'c1'")
            )
            assert result.one() == ("keepme", None, None)
            result = await conn.execute(text("SELECT executor FROM step_runs WHERE id = 'sr1'"))
            assert result.scalar_one() is None

    async def test_0004_roundtrip_drops_and_recreates_test_tieback(self, engine_factory):
        """head -> 0003 -> head: test_refs/test_runs rows are gone by design
        (whole-table drop), the tables come back empty, and rows in the
        surviving spec tables are untouched."""
        engine = engine_factory("rt_0004.db")
        await _migrate(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO repos (id, name, default_branch, is_ingested, created_at) "
                    "VALUES ('r1', 'repo', 'main', 0, '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO features (id, title, description, status, repo_ids, created_at, updated_at) "
                    "VALUES ('f1', 'feat', '', 'draft', '[]', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO user_stories (id, feature_id, title, narrative, status, created_at, updated_at) "
                    "VALUES ('us1', 'f1', 'story', '', 'accepted', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO acceptance_criteria (id, user_story_id, text, required, created_at, updated_at) "
                    "VALUES ('ac1', 'us1', 'crit', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO test_refs (id, lazyaf_test_id, repo_id, criterion_id, status, created_at, updated_at) "
                    "VALUES ('tr1', 'suite.case', 'r1', 'ac1', 'active', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO test_runs (id, test_ref_id, pipeline_run_id, commit_sha, status, created_at) "
                    "VALUES ('run1', 'tr1', 'pr1', 'abc123', 'passed', '2026-01-01 00:00:00')"
                )
            )
        head = await _snapshot(engine)

        await _downgrade_to(engine, "0003")
        await _upgrade_to(engine, "head")

        assert await _snapshot(engine) == head
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM test_refs"))
            assert result.scalar_one() == 0
            result = await conn.execute(text("SELECT COUNT(*) FROM test_runs"))
            assert result.scalar_one() == 0
            result = await conn.execute(text("SELECT text FROM acceptance_criteria WHERE id = 'ac1'"))
            assert result.scalar_one() == "crit"


class TestTieBackSchemaShape:
    """0004's test tie-back objects, pinned (contract #1 identity + the
    index set the hot paths actually use)."""

    async def test_test_ref_identity_is_repo_id_plus_lazyaf_test_id(
        self, engine_factory
    ):
        """The same marker string under two repos is two independent refs;
        the same string twice under ONE repo is a constraint violation."""
        engine = engine_factory("tieback_identity.db")
        await _migrate(engine)

        async with engine.begin() as conn:
            for repo_id in ("r1", "r2"):
                await conn.execute(
                    text(
                        "INSERT INTO repos (id, name, default_branch, is_ingested, created_at) "
                        f"VALUES ('{repo_id}', '{repo_id}', 'main', 0, '2026-01-01 00:00:00')"
                    )
                )
            # same lazyaf_test_id, different repos -> both accepted
            for ref_id, repo_id in (("ref1", "r1"), ("ref2", "r2")):
                await conn.execute(
                    text(
                        "INSERT INTO test_refs (id, lazyaf_test_id, repo_id, status, created_at, updated_at) "
                        f"VALUES ('{ref_id}', 'suite.case', '{repo_id}', 'active', "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    )
                )

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM test_refs WHERE lazyaf_test_id = 'suite.case'")
            )
            assert result.scalar_one() == 2

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO test_refs (id, lazyaf_test_id, repo_id, status, created_at, updated_at) "
                        "VALUES ('ref3', 'suite.case', 'r1', 'active', "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    )
                )

    async def test_tieback_indexes_are_exactly_the_access_paths(self, engine_factory):
        """Every index earns its write cost: the repo-scoped identity, the
        criterion join, the (test_ref_id, created_at) history/freshness walk
        and the step_run_id idempotency lookup — nothing else."""
        engine = engine_factory("tieback_indexes.db")
        await _migrate(engine)

        snapshot = await _snapshot(engine)
        assert snapshot["test_refs"]["indexes"] == {
            "ix_test_refs_criterion_id": (("criterion_id",), False),
            "ix_test_refs_repo_id_lazyaf_test_id": (
                ("repo_id", "lazyaf_test_id"),
                True,
            ),
        }
        assert snapshot["test_runs"]["indexes"] == {
            "ix_test_runs_test_ref_id_created_at": (
                ("test_ref_id", "created_at"),
                False,
            ),
            "ix_test_runs_step_run_id": (("step_run_id",), False),
        }


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
