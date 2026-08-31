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
# M14: model_endpoints is registered on Base.metadata by importing its module.
# This line becomes redundant the moment `app/models/__init__.py` exports
# ModelEndpoint (the registration edit A's report asks the integrator for) —
# but WITHOUT it, create_all here builds a schema missing the table that 0011
# creates, and the parity assertions below would fail for a reason that has
# nothing to do with the migration.
import app.models.model_endpoint  # noqa: F401
from app.database import ALEMBIC_BASELINE_REVISION, Base, _alembic_config, _run_migrations

# Tip of the migration chain. Every startup path (fresh upgrade, legacy
# adoption stamp-then-upgrade) must end here.
ALEMBIC_HEAD_REVISION = "0012"

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
    # 0005 (Phase 12.5 usage channel)
    "step_usages",
    # 0006 (Phase 12.6 runner registry) adds columns/indexes only - no tables
    # 0007 (Phase 12.6 deletion commit) drops columns only - no tables
    # 0008 was pre-assigned to 12.6.6 and released back to the pool unused
    # 0009 (Phase 12.7 debug re-run)
    "debug_sessions",
    # 0010 (Phase 12.6.5 experiments). Numbered 0010, not the design's 0008:
    # 0009 landed first parented on 0007, so taking 0008 off 0007 would fork
    # the chain into two heads and stop init_db dead. See the revision's own
    # docstring for the four-line path back to the design's ordering.
    "experiments",
    "experiment_runs",
    "prompt_versions",
    # 0011 (Milestone 14): self-hosted OpenAI-compatible endpoints. Adds one
    # table plus step_executions.model_endpoint_id; deliberately adds NOTHING
    # to step_usages (the endpoint join goes through gpu_node_id).
    "model_endpoints",
    # 0012 (M13-1) reshapes workspaces' uniqueness (a run owns one workspace
    # PER LANE) - one column plus an index swap, no tables.
}

SPEC_TABLES = {"features", "user_stories", "acceptance_criteria", "prompt_templates"}

TEST_TIEBACK_TABLES = {"test_refs", "test_runs"}

USAGE_TABLES = {"step_usages"}


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


async def _seed_usage_chain(engine):
    """A full repo -> pipeline -> run -> step -> execution -> usage chain,
    plus one TestRef, for the 0005 round-trip and identity tests."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO repos (id, name, default_branch, is_ingested, created_at) "
                "VALUES ('r1', 'repo', 'main', 0, '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO test_refs (id, lazyaf_test_id, repo_id, status, created_at, updated_at) "
                "VALUES ('tr1', 'suite.case', 'r1', 'active', '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, repo_id, name, steps, triggers, is_template, "
                "created_at, updated_at) "
                "VALUES ('p1', 'r1', 'pipe', '[]', '[]', 0, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')"
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
                "INSERT INTO step_runs (id, pipeline_run_id, step_index, step_name, status, logs) "
                "VALUES ('sr1', 'pr1', 0, 'agent', 'passed', '')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO step_executions (id, execution_key, step_run_id, status, created_at) "
                "VALUES ('se1', 'pr1:0:1', 'sr1', 'completed', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO step_usages (id, step_execution_id, step_run_id, pipeline_run_id, "
                "provider, cost_source, wall_clock_ms, determinism, created_at, updated_at) "
                "VALUES ('u1', 'se1', 'sr1', 'pr1', 'anthropic', 'cli-reported', 100, '{}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )


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
        # 0005's downgrade ran first on the way down.
        assert USAGE_TABLES.isdisjoint(set(snapshot))
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
        assert USAGE_TABLES.isdisjoint(set(snapshot))
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


class TestUsageChannelMigration:
    """0005's step_usages, pinned (Phase 12.5 usage channel).

    The two scope decisions this revision makes irreversible are asserted
    here, not just commented: `role` EXISTS on the same migration as the
    frozen wire (without it `cost_by_role` is unrecoverable), and
    `trial_iteration_id` does NOT (nothing writes it, no table to
    reference - it lands with M13's trials table).
    """

    async def test_downgrade_to_0004_removes_the_usage_table_only(
        self, engine_factory
    ):
        """0005's downgrade drops step_usages and nothing else: the test
        tie-back tables, the spec tables and workspaces all stay."""
        engine = engine_factory("down_0004.db")
        await _migrate(engine)

        await _downgrade_to(engine, "0004")

        snapshot = await _snapshot(engine)
        assert USAGE_TABLES.isdisjoint(set(snapshot))
        assert TEST_TIEBACK_TABLES <= set(snapshot)
        assert SPEC_TABLES <= set(snapshot)
        assert "workspaces" in snapshot
        assert await _alembic_versions(engine) == ["0004"]

    async def test_0005_roundtrip_drops_and_recreates_step_usages(
        self, engine_factory
    ):
        """head -> 0004 -> head: usage rows are gone by design (whole-table
        drop), the table comes back schema-identical and empty, and the
        test tie-back rows beside it are untouched."""
        engine = engine_factory("rt_0005.db")
        await _migrate(engine)
        await _seed_usage_chain(engine)
        head = await _snapshot(engine)

        await _downgrade_to(engine, "0004")
        await _upgrade_to(engine, "head")

        assert await _snapshot(engine) == head
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM step_usages"))
            assert result.scalar_one() == 0
            result = await conn.execute(text("SELECT COUNT(*) FROM test_refs"))
            assert result.scalar_one() == 1

    async def test_usage_indexes_are_exactly_the_access_paths(self, engine_factory):
        """Every index earns its write cost: the step_execution_id
        idempotency key (UNIQUE - a retrying runtime must be structurally
        unable to double-bill) and the (pipeline_run_id, role) rollup scan.
        Nothing else."""
        engine = engine_factory("usage_indexes.db")
        await _migrate(engine)

        snapshot = await _snapshot(engine)
        assert snapshot["step_usages"]["indexes"] == {
            "ix_step_usages_step_execution_id": (("step_execution_id",), True),
            "ix_step_usages_pipeline_run_id_role": (
                ("pipeline_run_id", "role"),
                False,
            ),
        }

    async def test_idempotency_key_is_enforced_by_the_database(self, engine_factory):
        """Two usage rows for one StepExecution is a constraint violation,
        not an application-level convention: double-billing must be
        structurally impossible."""
        engine = engine_factory("usage_identity.db")
        await _migrate(engine)
        await _seed_usage_chain(engine)

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO step_usages (id, step_execution_id, provider, cost_source, "
                        "wall_clock_ms, determinism, created_at, updated_at) "
                        "VALUES ('u2', 'se1', 'anthropic', 'cli-reported', 200, '{}', "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    )
                )

    async def test_role_ships_on_this_migration(self, engine_factory):
        """api-surface 2.6: `role` must exist on the SAME migration as the
        frozen wire, or cost_by_role is unrecoverable after the fact."""
        engine = engine_factory("usage_role.db")
        await _migrate(engine)

        columns = (await _snapshot(engine))["step_usages"]["columns"]
        assert "role" in columns

    async def test_trial_iteration_id_is_deliberately_absent(self, engine_factory):
        """Design 3.6: nothing writes it and there is no table to reference
        in 12.5; an orphan column buys nothing."""
        engine = engine_factory("usage_no_trial.db")
        await _migrate(engine)

        columns = (await _snapshot(engine))["step_usages"]["columns"]
        assert "trial_iteration_id" not in columns

    async def test_cost_usd_is_numeric_not_a_float_column(self, engine_factory):
        """Money is NUMERIC(18,6) in the DDL - the float column that would
        silently lose cents must never appear here."""
        engine = engine_factory("usage_money.db")
        await _migrate(engine)

        col_type, _nullable, _pk = (await _snapshot(engine))["step_usages"]["columns"][
            "cost_usd"
        ]
        assert "NUMERIC" in col_type.upper()


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
        criterion join, the (test_ref_id, created_at) history/freshness walk,
        the step_run_id idempotency lookup, and — since 12.6.5 — the
        (experiment_run_id, test_ref_id) leaderboard aggregation. Nothing
        else."""
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
            # 0010 (Phase 12.6.5): the leaderboard's per-criterion scan.
            "ix_test_runs_experiment_run_id_test_ref_id": (
                ("experiment_run_id", "test_ref_id"),
                False,
            ),
        }


class TestModelEndpointsMigration:
    """0011 (Milestone 14): the endpoint registry.

    The three decisions this revision makes irreversible are asserted here,
    not merely commented: the endpoint join goes through `gpu_node_id` (so
    `step_usages` gains NOTHING), the admission gate's column and composite
    index exist on `step_executions`, and no column can hold a secret VALUE.
    """

    async def test_model_endpoints_table_and_indexes(self, engine_factory):
        engine = engine_factory("endpoints_schema.db")
        await _migrate(engine)

        snapshot = await _snapshot(engine)
        assert snapshot["model_endpoints"]["indexes"] == {
            "ix_model_endpoints_name": (("name",), True),
            "ix_model_endpoints_gpu_node_id": (("gpu_node_id",), False),
            "ix_model_endpoints_enabled_reach": (("enabled", "reach"), False),
        }
        columns = snapshot["model_endpoints"]["columns"]
        # The join key into step_usages.gpu_node_id is NOT NULL: an endpoint
        # without a node coordinate could never be priced.
        assert columns["gpu_node_id"][1] is False
        # Money is NUMERIC, never a float column that silently loses cents.
        assert "NUMERIC" in columns["rate_usd_hour"][0].upper()

    async def test_capability_booleans_are_nullable(self, engine_factory):
        """THREE-STATE, at the DDL level. A NOT NULL `supports_tools` would
        force a default of False, which silently routes every new endpoint
        down the no-tools fallback protocol - the exact invisible downgrade
        R1 exists to forbid."""
        engine = engine_factory("endpoints_threestate.db")
        await _migrate(engine)

        columns = (await _snapshot(engine))["model_endpoints"]["columns"]
        for name in ("supports_tools", "supports_streaming", "reports_usage"):
            assert columns[name][1] is True, name

    async def test_no_column_can_hold_a_secret_value(self, engine_factory):
        """The database stores a REFERENCE (an env var NAME) and nothing
        else. A column called anything like `api_key`/`token`/`secret_value`
        appearing here later is the regression this pins."""
        engine = engine_factory("endpoints_secrets.db")
        await _migrate(engine)

        columns = set((await _snapshot(engine))["model_endpoints"]["columns"])
        assert "auth_secret_ref" in columns
        for forbidden in ("api_key", "auth_secret", "secret", "token", "auth_value"):
            assert forbidden not in columns, forbidden

    async def test_step_executions_gained_the_admission_gate_column(
        self, engine_factory
    ):
        """Contract #9: the in-flight count is READ FROM THE DATABASE, so the
        column and its composite index have to exist."""
        engine = engine_factory("endpoints_gate.db")
        await _migrate(engine)

        snapshot = await _snapshot(engine)
        assert "model_endpoint_id" in snapshot["step_executions"]["columns"]
        assert snapshot["step_executions"]["indexes"][
            "ix_step_executions_endpoint_status"
        ] == (("model_endpoint_id", "status"), False)

    async def test_step_usages_is_untouched_by_this_revision(self, engine_factory):
        """The endpoint join goes through `step_usages.gpu_node_id`. A
        materialized `model_endpoint_id` here would be a second writer for a
        fact the join already carries - and would make historical usage
        unpriceable once the endpoint row is deleted."""
        engine = engine_factory("endpoints_usage.db")
        await _migrate(engine)

        columns = (await _snapshot(engine))["step_usages"]["columns"]
        assert "model_endpoint_id" not in columns
        assert "gpu_node_id" in columns

    async def test_downgrade_to_0010_removes_this_revision_only(self, engine_factory):
        engine = engine_factory("endpoints_down.db")
        reference = engine_factory("endpoints_ref.db")
        await _migrate(engine)
        await _upgrade_to(reference, "0010")

        await _downgrade_to(engine, "0010")

        snapshot = await _snapshot(engine)
        assert "model_endpoints" not in snapshot
        assert "model_endpoint_id" not in snapshot["step_executions"]["columns"]
        assert "experiments" in snapshot
        assert snapshot["step_executions"] == (await _snapshot(reference))[
            "step_executions"
        ]
        assert await _alembic_versions(engine) == ["0010"]

    async def test_0011_roundtrip_restores_head(self, engine_factory):
        engine = engine_factory("endpoints_rt.db")
        await _migrate(engine)
        head = await _snapshot(engine)

        await _downgrade_to(engine, "0010")
        await _upgrade_to(engine, "head")

        assert await _snapshot(engine) == head
        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]

    async def test_upgrade_is_idempotent_over_a_healed_schema(self, engine_factory):
        """The adopt path: create_all builds the CURRENT model schema, the DB
        is stamped behind head, and 0011 then runs over objects that already
        exist. Every add is guarded, so this must not raise."""
        engine = engine_factory("endpoints_idem.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: command.stamp(_alembic_config(c), "0010"))

        await _upgrade_to(engine, "0011")

        assert await _alembic_versions(engine) == ["0011"]

    async def test_endpoint_name_is_unique(self, engine_factory):
        """The name is the handle every other surface uses
        (`model: "endpoint:<name>"`), so two rows sharing one is a
        constraint violation rather than an application convention."""
        engine = engine_factory("endpoints_unique.db")
        await _migrate(engine)

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO model_endpoints (id, name, base_url, model, "
                    "server_kind, auth_style, reach, gpu_node_id, max_concurrency, "
                    "request_timeout_seconds, probe_status, probe_detail, "
                    "consecutive_failures, enabled, created_at, updated_at) VALUES "
                    "('e1', 'local-4090', 'http://x/v1', 'qwen', 'ollama', 'none', "
                    "'direct', 'endpoint:local-4090', 1, 300, 'unprobed', '{}', 0, 1, "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO model_endpoints (id, name, base_url, model, "
                        "server_kind, auth_style, reach, gpu_node_id, max_concurrency, "
                        "request_timeout_seconds, probe_status, probe_detail, "
                        "consecutive_failures, enabled, created_at, updated_at) VALUES "
                        "('e2', 'local-4090', 'http://y/v1', 'llama', 'ollama', 'none', "
                        "'direct', 'endpoint:other', 1, 300, 'unprobed', '{}', 0, 1, "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    )
                )


def _workspace_row_sql(
    ws_id: str, run_id: str, volume_name: str, worker_key: str | None = None
) -> str:
    """INSERT for one workspaces row, with or without the 0012 lane column."""
    columns = "id, pipeline_run_id, repo_id, volume_name, status, use_count, created_at, updated_at"
    values = (
        f"'{ws_id}', '{run_id}', 'r1', '{volume_name}', 'ready', 0, "
        "'2026-01-01 00:00:00', '2026-01-01 00:00:00'"
    )
    if worker_key is not None:
        columns += ", worker_key"
        values += f", '{worker_key}'"
    return f"INSERT INTO workspaces ({columns}) VALUES ({values})"


class TestWorkspacePerWorkerMigration:
    """0012 (M13-1): a run owns one workspace PER LANE.

    0002 made `pipeline_run_id` UNIQUE — one workspace, one volume, one
    checkout per run. That is the constraint that makes the owner's
    headline hypothesis unmeasurable: K parallel agent steps all mount the
    same working tree, so any conflict rate a benchmark reports is a
    property of this schema rather than of the strategy under test.

    Two things are asserted here that comments cannot enforce: the lane
    column is NOT NULL (a nullable one would constrain nothing, since both
    SQLite and Postgres treat NULLs as distinct inside a unique index), and
    an existing row keeps its VOLUME NAME across the upgrade (no rename
    means no orphaned volume and no re-clone for a run in flight).
    """

    async def test_0012_adds_worker_key_not_null(self, engine_factory):
        engine = engine_factory("lanes_schema.db")
        await _migrate(engine)

        columns = (await _snapshot(engine))["workspaces"]["columns"]
        assert "worker_key" in columns
        type_str, nullable, _ = columns["worker_key"]
        assert nullable is False, (
            "a nullable lane column constrains NOTHING: NULLs are DISTINCT in "
            "a unique index, so the default-lane rows — the common case — "
            "would lose all duplicate protection"
        )
        assert "64" in type_str

    async def test_0012_swaps_the_run_index_for_the_composite_unique(
        self, engine_factory
    ):
        engine = engine_factory("lanes_index.db")
        await _migrate(engine)

        indexes = (await _snapshot(engine))["workspaces"]["indexes"]
        assert indexes["uq_workspaces_run_worker"] == (
            ("pipeline_run_id", "worker_key"),
            True,
        )
        # Dropped outright, not recreated non-unique: the composite index
        # leads with pipeline_run_id and already serves those lookups.
        assert "ix_workspaces_pipeline_run_id" not in indexes
        assert "ix_workspaces_repo_id" in indexes
        assert "ix_workspaces_status" in indexes

    async def test_0012_backfills_existing_rows_without_renaming_volumes(
        self, engine_factory
    ):
        """The upgrade must not strand a run that is in flight. Because the
        default lane emits NO name suffix, the backfilled row's volume_name
        is the string already on the live docker volume."""
        engine = engine_factory("lanes_backfill.db")
        await _upgrade_to(engine, "0011")
        run_id = "pr-legacy"
        volume_name = f"lazyaf-ws-{run_id}"
        async with engine.begin() as conn:
            await conn.execute(text(_workspace_row_sql("w-legacy", run_id, volume_name)))

        await _upgrade_to(engine, "0012")

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT worker_key, volume_name FROM workspaces "
                        "WHERE id = 'w-legacy'"
                    )
                )
            ).one()
        assert row[0] == "default"
        assert row[1] == volume_name

    async def test_two_lanes_of_one_run_are_insertable_after_0012(
        self, engine_factory
    ):
        """The whole point: K parallel workers, K independent checkouts."""
        engine = engine_factory("lanes_two.db")
        await _migrate(engine)

        async with engine.begin() as conn:
            await conn.execute(
                text(_workspace_row_sql("w1", "pr1", "lazyaf-ws-pr1-w1", "w1"))
            )
            await conn.execute(
                text(_workspace_row_sql("w2", "pr1", "lazyaf-ws-pr1-w2", "w2"))
            )

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM workspaces WHERE pipeline_run_id = 'pr1'")
                )
            ).scalar()
        assert count == 2

    async def test_a_duplicate_lane_is_rejected_after_0012(self, engine_factory):
        """Two rows for one lane means two volumes and two clones, one of
        which nothing will ever find again, release, or clean. The in-process
        lock is single-process by design, so the database is the backstop."""
        engine = engine_factory("lanes_dup.db")
        await _migrate(engine)

        async with engine.begin() as conn:
            await conn.execute(
                text(_workspace_row_sql("w1", "pr1", "lazyaf-ws-pr1-w1", "w1"))
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(_workspace_row_sql("w1b", "pr1", "lazyaf-ws-other", "w1"))
                )

    async def test_two_default_lane_rows_for_one_run_are_still_rejected(
        self, engine_factory
    ):
        """The regression a nullable lane column would have introduced: the
        common case must keep exactly the protection 0002 gave it."""
        engine = engine_factory("lanes_dup_default.db")
        await _migrate(engine)

        async with engine.begin() as conn:
            await conn.execute(
                text(_workspace_row_sql("d1", "pr1", "lazyaf-ws-pr1", "default"))
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(_workspace_row_sql("d2", "pr1", "lazyaf-ws-pr1-b", "default"))
                )

    async def test_downgrade_to_0011_restores_the_single_workspace_shape(
        self, engine_factory
    ):
        engine = engine_factory("lanes_down.db")
        reference = engine_factory("lanes_ref.db")
        await _migrate(engine)
        await _upgrade_to(reference, "0011")

        await _downgrade_to(engine, "0011")

        snapshot = await _snapshot(engine)
        assert "worker_key" not in snapshot["workspaces"]["columns"]
        assert snapshot["workspaces"]["indexes"]["ix_workspaces_pipeline_run_id"] == (
            ("pipeline_run_id",),
            True,
        )
        assert "uq_workspaces_run_worker" not in snapshot["workspaces"]["indexes"]
        assert snapshot["workspaces"] == (await _snapshot(reference))["workspaces"]
        assert await _alembic_versions(engine) == ["0011"]

    async def test_downgrade_drops_non_default_lanes_and_keeps_the_trunk(
        self, engine_factory
    ):
        """DESTRUCTIVE and deliberately so: the old shape cannot represent a
        per-worker lane, and leaving those rows would make the single-column
        UNIQUE index fail to build. Their volumes become unmatched, which the
        orphan audit's third sweep reaps. The default lane is untouched."""
        engine = engine_factory("lanes_down_data.db")
        await _migrate(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(_workspace_row_sql("d1", "pr1", "lazyaf-ws-pr1", "default"))
            )
            await conn.execute(
                text(_workspace_row_sql("w1", "pr1", "lazyaf-ws-pr1-w1", "w1"))
            )
            await conn.execute(
                text(_workspace_row_sql("w2", "pr1", "lazyaf-ws-pr1-w2", "w2"))
            )

        await _downgrade_to(engine, "0011")

        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT id FROM workspaces ORDER BY id"))
            ).scalars().all()
        assert list(rows) == ["d1"]

    async def test_0012_roundtrip_restores_head(self, engine_factory):
        engine = engine_factory("lanes_rt.db")
        await _migrate(engine)
        head = await _snapshot(engine)

        await _downgrade_to(engine, "0011")
        await _upgrade_to(engine, "head")

        assert await _snapshot(engine) == head
        assert await _alembic_versions(engine) == [ALEMBIC_HEAD_REVISION]

    async def test_upgrade_is_idempotent_over_a_healed_schema(self, engine_factory):
        """The adopt path: create_all builds the CURRENT model schema — lane
        column and composite index included — the DB is stamped behind head,
        and 0012 then runs over objects that already exist. Every step is
        guarded, so this must not raise."""
        engine = engine_factory("lanes_idem.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: command.stamp(_alembic_config(c), "0011"))

        await _upgrade_to(engine, "0012")

        assert await _alembic_versions(engine) == ["0012"]
        indexes = (await _snapshot(engine))["workspaces"]["indexes"]
        assert "uq_workspaces_run_worker" in indexes
        assert "ix_workspaces_pipeline_run_id" not in indexes


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


class TestRunnerRegistryMigration:
    """0006 (Phase 12.6): the runners table stops being dead.

    ADD ONLY - `container_id` and `current_job_id` survive until 0007, in the
    deletion commit. The data migration is the load-bearing half: the old
    RunnerStatus vocabulary ('offline') has to become the RunnerState one
    ('disconnected'), and no connection may appear to survive a migration.
    """

    async def _seed_runners_at_0005(self, engine):
        await _upgrade_to(engine, "0005")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runners (id, container_id, status, current_job_id, "
                    "last_heartbeat) VALUES "
                    "('r-offline', 'abc123456789', 'offline', NULL, '2026-01-01 00:00:00')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO runners (id, container_id, status, current_job_id, "
                    "last_heartbeat) VALUES "
                    "('r-busy', 'def456789012', 'busy', 'job-1', '2026-01-01 00:00:00')"
                )
            )

    async def _runner_rows(self, engine):
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, status, websocket_id, current_step_execution_id, "
                    "runner_type, container_id FROM runners ORDER BY id"
                )
            )
            return {row[0]: row for row in result.all()}

    async def test_offline_becomes_disconnected(self, engine_factory):
        engine = engine_factory("runner_0006_status.db")
        await self._seed_runners_at_0005(engine)

        await _upgrade_to(engine, "0006")

        rows = await self._runner_rows(engine)
        assert rows["r-offline"][1] == "disconnected"

    async def test_every_row_lands_disconnected_with_no_socket(self, engine_factory):
        """No connection survives a migration; pretending one did is how a
        fresh backend hands work to a ghost."""
        engine = engine_factory("runner_0006_ghost.db")
        await self._seed_runners_at_0005(engine)

        await _upgrade_to(engine, "0006")

        rows = await self._runner_rows(engine)
        assert {row[1] for row in rows.values()} == {"disconnected"}
        assert all(row[2] is None for row in rows.values())  # websocket_id
        assert all(row[3] is None for row in rows.values())  # current_step_execution_id

    async def test_rows_and_polling_columns_survive_the_rebuild(self, engine_factory):
        """The runners table is rebuilt in batch mode (SQLite cannot ALTER a
        FK into place); the data has to come through it."""
        engine = engine_factory("runner_0006_data.db")
        await self._seed_runners_at_0005(engine)

        await _upgrade_to(engine, "0006")

        rows = await self._runner_rows(engine)
        assert set(rows) == {"r-offline", "r-busy"}
        assert rows["r-busy"][4] == "claude-code"  # runner_type server_default
        assert rows["r-busy"][5] == "def456789012"  # container_id, dropped in 0007

    async def test_new_columns_and_indexes_exist(self, engine_factory):
        engine = engine_factory("runner_0006_schema.db")
        await _migrate(engine)

        snapshot = await _snapshot(engine)
        columns = snapshot["runners"]["columns"]
        for name in (
            "name",
            "runner_type",
            "labels",
            "current_step_execution_id",
            "websocket_id",
            "protocol_version",
            "agent_version",
            "connected_at",
            "created_at",
        ):
            assert name in columns, name
        indexes = snapshot["runners"]["indexes"]
        assert indexes["ix_runners_websocket_id"] == (("websocket_id",), True)
        assert indexes["ix_runners_status"] == (("status",), False)

    async def test_step_executions_gained_the_dispatcher_columns(self, engine_factory):
        """A requeued step must be re-matchable AFTER a backend restart, so
        the requires: block has to be durable, not held in a dispatch
        closure."""
        engine = engine_factory("runner_0006_steps.db")
        await _migrate(engine)

        snapshot = await _snapshot(engine)
        assert "runner_requirements" in snapshot["step_executions"]["columns"]
        assert "assigned_at" in snapshot["step_executions"]["columns"]
        assert "ix_step_executions_status" in snapshot["step_executions"]["indexes"]

    async def test_upgrade_is_idempotent_over_a_healed_schema(self, engine_factory):
        """The adopt path: create_all builds the CURRENT model schema, the DB
        is stamped behind head, and 0006 then runs over objects that already
        exist. Every add is guarded, so this must not raise."""
        engine = engine_factory("runner_0006_idem.db")
        await _create_all(engine)
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: command.stamp(_alembic_config(c), "0005"))

        await _upgrade_to(engine, "0006")

        assert await _alembic_versions(engine) == ["0006"]

    async def test_downgrade_restores_the_0005_runners_shape(self, engine_factory):
        engine = engine_factory("runner_0006_down.db")
        reference = engine_factory("runner_0005_ref.db")
        await _migrate(engine)
        await _upgrade_to(reference, "0005")

        await _downgrade_to(engine, "0005")

        after = await _snapshot(engine)
        expected = await _snapshot(reference)
        assert after["runners"] == expected["runners"]
        assert after["step_executions"] == expected["step_executions"]
