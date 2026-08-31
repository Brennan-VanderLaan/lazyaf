"""Migration 0014: the v1 array backfill and the `definition_error` column.

Phase 12.8 P4. This suite is deliberately SEPARATE from
`tdd/integration/test_migrations.py`, which owns the chain-wide invariants
(head pin, fresh-vs-create_all parity, baseline round trip). What lives here
is everything specific to retiring the v1 array pipeline format, and the one
property the rest of the wave rests on:

    after 0014, no pipeline row in any database has an array definition and
    no graph.

Every test drives the REAL revision through alembic against a real SQLite
file, because the thing under test is a data migration and a data migration
that is unit-tested against its own helper functions is a transcript. The
frozen converter is exercised through `command.upgrade` in almost every
case; only the fidelity test in `TestTheConverterIsFrozen` reaches into the
module directly, and it says why.
"""
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from app.database import _alembic_config

#: The revision under test, and the one before it. Named rather than
#: inlined so a future renumbering is one edit.
REVISION = "0014"
PARENT = "0013"


# -----------------------------------------------------------------------------
# Harness
# -----------------------------------------------------------------------------

def _engine(tmp_path: Path, name: str = "scratch.db") -> sa.Engine:
    """A real SQLite file database. Not `sqlite://`.

    The revision uses `op.batch_alter_table` on downgrade, which rebuilds the
    table; an in-memory database is fine for that too, but a file is what
    every deployment actually is and it costs nothing here.
    """
    return sa.create_engine(f"sqlite:///{tmp_path / name}")


def _upgrade(engine: sa.Engine, revision: str) -> None:
    with engine.begin() as conn:
        command.upgrade(_alembic_config(conn), revision)


def _downgrade(engine: sa.Engine, revision: str) -> None:
    with engine.begin() as conn:
        command.downgrade(_alembic_config(conn), revision)


def _seed_repo(engine: sa.Engine, repo_id: str = "repo-1") -> str:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO repos (id, name, default_branch, is_ingested, "
                "created_at) VALUES (:id, 'demo', 'main', 1, '2026-08-31')"
            ),
            {"id": repo_id},
        )
    return repo_id


def _seed_pipeline(
    engine: sa.Engine,
    pipeline_id: str,
    *,
    name: str = "A Pipeline",
    steps: str = "[]",
    steps_graph: str | None = None,
    repo_id: str = "repo-1",
) -> str:
    """Insert a pipeline row exactly as the pre-12.8 writers did.

    `steps` is a raw string on purpose - several tests need to plant a value
    that no writer would produce (unparseable JSON, an object instead of an
    array), which is the whole point of the refusal path.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO pipelines (id, repo_id, name, steps, steps_graph, "
                "triggers, is_template, created_at, updated_at) VALUES "
                "(:id, :repo_id, :name, :steps, :graph, '[]', 0, "
                "'2026-08-31', '2026-08-31')"
            ),
            {
                "id": pipeline_id,
                "repo_id": repo_id,
                "name": name,
                "steps": steps,
                "graph": steps_graph,
            },
        )
    return pipeline_id


def _row(engine: sa.Engine, pipeline_id: str) -> dict:
    with engine.connect() as conn:
        result = conn.execute(
            sa.text("SELECT * FROM pipelines WHERE id = :id"),
            {"id": pipeline_id},
        ).mappings().one()
    return dict(result)


def _graph(engine: sa.Engine, pipeline_id: str) -> dict:
    raw = _row(engine, pipeline_id)["steps_graph"]
    assert raw, f"pipeline {pipeline_id} has no graph: {raw!r}"
    return json.loads(raw)


def _columns(engine: sa.Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {col["name"] for col in sa.inspect(conn).get_columns(table)}


def _array(*steps: dict) -> str:
    return json.dumps(list(steps))


def _step(name: str, **overrides) -> dict:
    step = {"name": name, "type": "script", "config": {"command": f"run {name}"}}
    step.update(overrides)
    return step


@pytest.fixture
def at_parent(tmp_path):
    """A database at the revision immediately BEFORE the one under test.

    Seeding happens here, so every backfill test plants its rows in a schema
    that genuinely predates the column and then watches the real revision
    run over them.
    """
    engine = _engine(tmp_path)
    _upgrade(engine, PARENT)
    _seed_repo(engine)
    try:
        yield engine
    finally:
        engine.dispose()


# -----------------------------------------------------------------------------
# The chain
# -----------------------------------------------------------------------------

class TestTheChainStaysLinear:
    """A fork in the chain is a dead backend, not a test failure.

    `_run_migrations` ends in `command.upgrade(config, "head")`, which
    REFUSES when there is more than one head. The suite has always pinned the
    head's value and never that there is only one of it, so a revision
    authored off a stale parent (the exact hazard when three waves are
    numbering revisions at once) would pass every existing assertion and
    brick startup.
    """

    def test_there_is_exactly_one_head(self, tmp_path):
        engine = _engine(tmp_path)
        try:
            with engine.connect() as conn:
                heads = ScriptDirectory.from_config(
                    _alembic_config(conn)
                ).get_heads()
        finally:
            engine.dispose()
        assert len(heads) == 1, (
            f"the migration chain has forked into {len(heads)} heads: "
            f"{sorted(heads)}. `command.upgrade(config, 'head')` refuses a "
            "multi-head chain, so this is a backend that cannot start"
        )

    def test_this_revision_is_reachable_and_parented_where_it_says(self, tmp_path):
        engine = _engine(tmp_path)
        try:
            with engine.connect() as conn:
                script = ScriptDirectory.from_config(_alembic_config(conn))
            revision = script.get_revision(REVISION)
        finally:
            engine.dispose()
        assert revision.down_revision == PARENT
        assert REVISION in script.get_heads() or revision.nextrev, (
            f"{REVISION} is neither a head nor has a child - it is orphaned"
        )


# -----------------------------------------------------------------------------
# The column
# -----------------------------------------------------------------------------

class TestDefinitionErrorColumn:
    """s1.7: the one channel a conversion refusal can surface on.

    Without it `upsert_materialized_pipeline` sets a python attribute that
    never reaches the database and the whole "refuse loudly" strategy is a
    fresh R1 violation.
    """

    def test_the_column_arrives_nullable(self, at_parent):
        assert "definition_error" not in _columns(at_parent, "pipelines")

        _upgrade(at_parent, REVISION)

        with at_parent.connect() as conn:
            column = next(
                col
                for col in sa.inspect(conn).get_columns("pipelines")
                if col["name"] == "definition_error"
            )
        assert column["nullable"] is True, (
            "a NOT NULL definition_error would make every INSERT that does "
            "not name it fail - and the normal case is having no error"
        )

    def test_an_existing_row_gets_a_null_error_not_an_empty_string(self, at_parent):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("build")))

        _upgrade(at_parent, REVISION)

        assert _row(at_parent, "p1")["definition_error"] is None, (
            "NULL means 'this definition is fine'; an empty string is a "
            "second spelling of the same thing and the run guards test "
            "truthiness"
        )

    def test_the_column_holds_a_refusal_message(self, at_parent):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("build")))
        _upgrade(at_parent, REVISION)

        with at_parent.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE pipelines SET definition_error = :msg WHERE id = 'p1'"
                ),
                {"msg": "step 'a' declares on_success='banana': unknown action"},
            )

        assert "banana" in _row(at_parent, "p1")["definition_error"]

    def test_the_revision_is_rerunnable_over_a_healed_database(self, at_parent):
        """The adopt path heals with create_all BEFORE stamping, so the
        column can already exist when this revision runs. 0007's idiom."""
        _seed_pipeline(at_parent, "p1", steps=_array(_step("build")))
        with at_parent.begin() as conn:
            conn.execute(
                sa.text("ALTER TABLE pipelines ADD COLUMN definition_error TEXT")
            )

        _upgrade(at_parent, REVISION)  # must not raise "duplicate column name"

        assert "definition_error" in _columns(at_parent, "pipelines")
        assert _graph(at_parent, "p1")["version"] == 2


# -----------------------------------------------------------------------------
# The backfill
# -----------------------------------------------------------------------------

class TestPipelineStepsBackfill:
    """s5.1's migration gate. After this revision, no row has an array and
    no graph."""

    def test_an_array_becomes_a_graph_keyed_by_the_authors_ids(self, at_parent):
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(
                _step("Sync Dependencies", id="sync-deps"),
                _step("T1", id="tier1"),
                _step("Verify", id="verify-executor"),
            ),
        )

        _upgrade(at_parent, REVISION)

        graph = _graph(at_parent, "p1")
        assert graph["version"] == 2
        assert graph["entry_points"] == ["sync-deps"]
        assert list(graph["steps"]) == ["sync-deps", "tier1", "verify-executor"]
        assert graph["steps"]["tier1"]["name"] == "T1"
        assert graph["steps"]["tier1"]["config"] == {"command": "run T1"}

    def test_a_step_without_an_id_gets_the_generated_position_key(self, at_parent):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a"), _step("b")))

        _upgrade(at_parent, REVISION)

        assert list(_graph(at_parent, "p1")["steps"]) == ["step_0", "step_1"]

    def test_consecutive_next_becomes_one_success_edge_per_pair(self, at_parent):
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(
                _step("a", id="a", on_success="next", on_failure="stop"),
                _step("b", id="b", on_success="next", on_failure="stop"),
                _step("c", id="c", on_success="next", on_failure="stop"),
            ),
        )

        _upgrade(at_parent, REVISION)

        edges = _graph(at_parent, "p1")["edges"]
        assert [(e["from_step"], e["to_step"], e["condition"]) for e in edges] == [
            ("a", "b", "success"),
            ("b", "c", "success"),
        ], (
            "three steps chained by `next` are two edges: the trailing "
            "`next` on the LAST step was a no-op in v1 (`_execute_step` "
            "guarded continuation with current_step + 1 < len(steps)) and "
            "must stay one here, or every pipeline in every database "
            "becomes unconvertible"
        )

    def test_a_stop_on_the_final_step_is_simply_no_edge(self, at_parent):
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(
                _step("a", id="a"),
                _step("b", id="b", on_success="stop", on_failure="stop"),
            ),
        )

        _upgrade(at_parent, REVISION)

        graph = _graph(at_parent, "p1")
        assert [e["from_step"] for e in graph["edges"]] == ["a"]
        assert set(graph["steps"]) == {"a", "b"}

    def test_positions_are_the_vertical_layout_the_editor_expects(self, at_parent):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a"), _step("b")))

        _upgrade(at_parent, REVISION)

        steps = _graph(at_parent, "p1")["steps"]
        assert steps["step_0"]["position"] == {"x": 100.0, "y": 0.0}
        assert steps["step_1"]["position"] == {"x": 100.0, "y": 150.0}

    def test_step_settings_survive_the_conversion(self, at_parent):
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(
                _step("a", id="a", timeout=1800, continue_in_context=True),
                _step("b", id="b", type="docker", config={"image": "python:3.12"}),
            ),
        )

        _upgrade(at_parent, REVISION)

        steps = _graph(at_parent, "p1")["steps"]
        assert steps["a"]["timeout"] == 1800
        assert steps["a"]["continue_in_context"] is True
        assert steps["b"]["type"] == "docker"
        assert steps["b"]["config"] == {"image": "python:3.12"}

    def test_defaults_are_supplied_for_a_step_that_omits_them(self, at_parent):
        """A persisted row may predate a field. The pydantic path supplied
        `on_success='next'` / `on_failure='stop'` / `timeout=300` /
        `continue_in_context=False`; reading raw dicts, the revision has to."""
        _seed_pipeline(
            at_parent,
            "p1",
            steps=json.dumps(
                [{"name": "a", "type": "script"}, {"name": "b", "type": "script"}]
            ),
        )

        _upgrade(at_parent, REVISION)

        graph = _graph(at_parent, "p1")
        assert graph["steps"]["step_0"]["timeout"] == 300
        assert graph["steps"]["step_0"]["continue_in_context"] is False
        assert [e["condition"] for e in graph["edges"]] == ["success"], (
            "an omitted on_success defaults to 'next' (an edge) and an "
            "omitted on_failure to 'stop' (no edge)"
        )

    def test_a_row_that_already_has_a_graph_keeps_it_byte_for_byte(self, at_parent):
        """Skip means SKIP. A row whose graph the author wrote must come out
        of this revision unchanged - not re-serialized, not re-ordered, not
        'normalised'."""
        authored = (
            '{"version": 2, "entry_points": ["only"], "edges": [], '
            '"steps": {"only": {"id": "only", "name": "Only", '
            '"type": "script", "config": {}, "position": null, '
            '"timeout": 42, "continue_in_context": false, '
            '"actions": {"success": [], "failure": [], "always": []}}}}'
        )
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(_step("a"), _step("b")),
            steps_graph=authored,
        )

        _upgrade(at_parent, REVISION)

        assert _row(at_parent, "p1")["steps_graph"] == authored

    def test_an_empty_string_graph_is_not_a_graph(self, at_parent):
        """`Pipeline.has_graph_definition()` tested `!= ''` for a reason:
        a row holding the empty string has no definition however NOT NULL
        the column looks."""
        _seed_pipeline(
            at_parent, "p1", steps=_array(_step("a", id="a")), steps_graph=""
        )

        _upgrade(at_parent, REVISION)

        assert _graph(at_parent, "p1")["entry_points"] == ["a"]

    def test_a_row_with_neither_definition_stays_alive_with_a_null_graph(
        self, at_parent
    ):
        """s4.7: there is no legal alternative. An 'empty graph' is
        unrepresentable - `validate_graph_integrity` rejects empty
        `entry_points` - so the row keeps NULL and stays editable rather than
        being invented into something runnable, or deleted."""
        _seed_pipeline(at_parent, "empty", name="Created, not yet authored", steps="[]")

        _upgrade(at_parent, REVISION)

        row = _row(at_parent, "empty")
        assert row["steps_graph"] is None
        assert row["name"] == "Created, not yet authored"
        assert row["definition_error"] is None, (
            "having no definition yet is not an ERROR - the editor's "
            "create-then-author flow produces exactly this row"
        )

    def test_a_blank_steps_value_is_treated_as_no_definition_not_as_corruption(
        self, at_parent
    ):
        _seed_pipeline(at_parent, "blank", steps="")

        _upgrade(at_parent, REVISION)

        assert _row(at_parent, "blank")["steps_graph"] is None

    def test_a_rerun_after_the_column_is_gone_is_a_noop_that_still_reports(
        self, at_parent, caplog
    ):
        """0015 drops `steps`. A database stamped back below 0014 after that
        (the adopt path can do exactly this) must not die on a SELECT of a
        column that no longer exists - and must not exit silently claiming it
        backfilled something either.
        """
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a", id="a")))
        _seed_pipeline(at_parent, "undefined", steps="[]")
        _upgrade(at_parent, REVISION)
        with at_parent.begin() as conn:
            conn.execute(sa.text("ALTER TABLE pipelines DROP COLUMN steps"))
            conn.execute(
                sa.text("UPDATE alembic_version SET version_num = :p"),
                {"p": PARENT},
            )

        with caplog.at_level("INFO", logger="alembic.runtime.migration"):
            _upgrade(at_parent, REVISION)

        assert _graph(at_parent, "p1")["entry_points"] == ["a"]
        messages = [record.getMessage() for record in caplog.records]
        assert any("no v1 array to backfill" in m for m in messages)
        assert any(
            "1 pipeline(s) have no graph" in m
            for m in messages
        ), (
            "the one branch that cannot do its job must say what it left "
            "undefined, not exit quietly"
        )

    def test_the_backfill_is_rerunnable(self, at_parent):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a", id="a")))
        _upgrade(at_parent, REVISION)
        first = _row(at_parent, "p1")["steps_graph"]

        # Re-running the revision body is what an adopted database does when
        # it is stamped at a parent it has already passed.
        with at_parent.begin() as conn:
            conn.execute(sa.text("UPDATE alembic_version SET version_num = :p"),
                         {"p": PARENT})
        _upgrade(at_parent, REVISION)

        assert _row(at_parent, "p1")["steps_graph"] == first


class TestTheBackfilledGraphIsRunnable:
    """The property the rest of the wave rests on, asserted by the
    authorities rather than by eye.

    Every other test here reads keys out of the JSON. This one hands the
    backfilled value to the two things that actually decide whether a
    definition works: `PipelineGraphModel`, which is what
    `PipelineRead.steps_graph` parses it with (an unparseable value 500s
    every pipeline endpoint), and `graph_definition_errors`, which is the
    executor's own definition-time check. A backfill that produced valid
    JSON the executor then refused would pass a shape test and still leave
    the deployment unable to run anything.
    """

    @pytest.mark.parametrize(
        "steps",
        [
            pytest.param([_step("only", id="only")], id="single-step"),
            pytest.param(
                [
                    _step("a", id="sync-deps"),
                    _step("b", id="tier1"),
                    _step("c", id="verify-executor"),
                ],
                id="linear-with-authored-ids",
            ),
            pytest.param(
                [_step("a"), _step("b", type="agent", config={"agent": "mock"})],
                id="generated-ids",
            ),
            pytest.param(
                [
                    _step("a", id="a", on_success="merge:main",
                          on_failure="trigger:card-9"),
                    _step("b", id="b"),
                ],
                id="both-effects",
            ),
        ],
    )
    def test_the_backfilled_value_parses_and_has_no_defects(
        self, at_parent, steps
    ):
        from app.schemas.pipeline import PipelineGraphModel
        from app.services.pipeline_executor import graph_definition_errors

        _seed_pipeline(at_parent, "p1", steps=json.dumps(steps))

        _upgrade(at_parent, REVISION)

        raw = _row(at_parent, "p1")["steps_graph"]
        graph = json.loads(raw)
        PipelineGraphModel.model_validate(graph)
        assert graph_definition_errors(graph) == []


class TestActionsAreNeverDroppedSilently:
    """The defect this whole milestone exists to prevent, in the one place
    that touches every historical row at once.

    Before 12.8 `array_to_graph` emitted an edge only for the literal string
    `"next"` and discarded `merge:` / `trigger:` on the floor - and its
    `if i < len(steps) - 1` guard meant an action on the LAST step, the
    common "merge when this passes" shape, was never even examined. A
    migration that reproduced that would silently strip card auto-fix and
    branch merging from every pipeline in every deployment simultaneously.
    """

    def test_a_merge_on_the_final_step_becomes_a_node_action(self, at_parent):
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(
                _step("build", id="build"),
                _step("test", id="test", on_success="merge:main"),
            ),
        )

        _upgrade(at_parent, REVISION)

        graph = _graph(at_parent, "p1")
        assert graph["steps"]["test"]["actions"]["success"] == ["merge:main"]
        assert [e["from_step"] for e in graph["edges"]] == ["build"], (
            "the final step's effect fires but leads nowhere - v1's "
            "continuation guard made the same call"
        )

    def test_a_merge_mid_array_keeps_the_effect_AND_the_edge(self, at_parent):
        """v1's `_merge_branch` fired and then continued to
        `current_step + 1`. Dropping either half is lossy in a different
        direction: no action loses the merge, no edge truncates the
        pipeline."""
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(
                _step("build", id="build", on_success="merge:release"),
                _step("deploy", id="deploy"),
            ),
        )

        _upgrade(at_parent, REVISION)

        graph = _graph(at_parent, "p1")
        assert graph["steps"]["build"]["actions"]["success"] == ["merge:release"]
        assert [
            (e["from_step"], e["to_step"], e["condition"]) for e in graph["edges"]
        ] == [("build", "deploy", "success")]

    def test_a_trigger_on_failure_becomes_a_failure_action_and_a_failure_edge(
        self, at_parent
    ):
        _seed_pipeline(
            at_parent,
            "p1",
            steps=_array(
                _step("test", id="test", on_failure="trigger:card-7"),
                _step("retest", id="retest"),
            ),
        )

        _upgrade(at_parent, REVISION)

        graph = _graph(at_parent, "p1")
        assert graph["steps"]["test"]["actions"]["failure"] == ["trigger:card-7"]
        assert graph["steps"]["test"]["actions"]["success"] == []
        assert ("test", "retest", "failure") in [
            (e["from_step"], e["to_step"], e["condition"]) for e in graph["edges"]
        ]

    def test_the_retired_pipeline_trigger_raises_naming_its_replacement(
        self, at_parent
    ):
        """s1.5 retires `trigger:pipeline:`. A migration may not convert it
        to something else and may not drop it: both would decide, on the
        operator's behalf, what a pipeline they wrote now means."""
        _seed_pipeline(
            at_parent,
            "chained",
            name="Chains another pipeline",
            steps=_array(
                _step("a", id="a", on_success="trigger:pipeline:other-id"),
                _step("b", id="b"),
            ),
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        message = str(excinfo.value)
        assert "chained" in message and "Chains another pipeline" in message
        assert "trigger:pipeline:" in message
        assert "card_complete" in message, "the refusal must name the replacement"

    def test_an_unknown_action_raises_rather_than_being_dropped(self, at_parent):
        _seed_pipeline(
            at_parent,
            "typo",
            name="Has a typo",
            steps=_array(_step("a", id="a", on_success="mrege:main")),
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        assert "mrege:main" in str(excinfo.value)
        assert "typo" in str(excinfo.value)

    def test_an_empty_merge_target_raises(self, at_parent):
        _seed_pipeline(
            at_parent, "p1", steps=_array(_step("a", id="a", on_success="merge:"))
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        assert "empty" in str(excinfo.value)


class TestRefusals:
    """A definition this cannot hold faithfully stops the upgrade and names
    itself. It is never half-converted and never quietly skipped."""

    def test_unparseable_steps_with_no_graph_raises_and_names_the_pipeline(
        self, at_parent
    ):
        """This value is invisible to the running application - every reader
        swallowed a JSONDecodeError to `[]` or `None` - so this revision is
        the first thing that has ever looked at it."""
        _seed_pipeline(
            at_parent, "broken", name="Corrupt Row", steps="{not json at all"
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        message = str(excinfo.value)
        assert "broken" in message
        assert "Corrupt Row" in message
        assert "not valid JSON" in message

    def test_unparseable_steps_WITH_a_graph_is_skipped_not_raised(self, at_parent):
        """The graph is the definition. A row that already has one is not
        blocked on the legibility of an array nothing will ever read again."""
        authored = json.dumps(
            {
                "version": 2,
                "entry_points": ["only"],
                "edges": [],
                "steps": {
                    "only": {
                        "id": "only",
                        "name": "Only",
                        "type": "script",
                        "config": {},
                        "position": None,
                        "timeout": 300,
                        "continue_in_context": False,
                        "actions": {"success": [], "failure": [], "always": []},
                    }
                },
            }
        )
        _seed_pipeline(at_parent, "p1", steps="{not json", steps_graph=authored)

        _upgrade(at_parent, REVISION)

        assert _row(at_parent, "p1")["steps_graph"] == authored

    def test_steps_holding_an_object_instead_of_an_array_raises(self, at_parent):
        _seed_pipeline(
            at_parent, "shape", name="Wrong Shape", steps='{"steps": []}'
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        assert "not a JSON array" in str(excinfo.value)
        assert "shape" in str(excinfo.value)

    def test_a_step_with_an_unknown_type_raises(self, at_parent):
        """`PipelineGraphModel` would refuse to parse the node back out of
        the column, so writing it would produce a row the application 500s
        on - a quieter failure than this one, discovered later."""
        _seed_pipeline(
            at_parent,
            "banana",
            steps=json.dumps([{"name": "a", "type": "banana"}]),
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        assert "banana" in str(excinfo.value)

    def test_a_mid_array_stop_raises_naming_the_step_that_orphaned_the_tail(
        self, at_parent
    ):
        """s1.6a. Emitting no edge would leave the tail unreachable and the
        run would fail at execution time for the wrong reason; truncating the
        tail would delete steps the author wrote."""
        _seed_pipeline(
            at_parent,
            "orphan",
            name="Stops In The Middle",
            steps=_array(
                _step("a", id="a", on_success="stop", on_failure="stop"),
                _step("b", id="b"),
                _step("c", id="c"),
            ),
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        message = str(excinfo.value)
        assert "'a'" in message, "the refusal must name the step responsible"
        assert "unreachable" in message
        assert "2 step(s)" in message

    def test_duplicate_ids_raise_rather_than_one_step_overwriting_the_other(
        self, at_parent
    ):
        _seed_pipeline(
            at_parent,
            "dupes",
            steps=_array(_step("first", id="same"), _step("second", id="same")),
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        assert "duplicate step id 'same'" in str(excinfo.value)

    def test_an_authored_id_colliding_with_a_generated_one_raises(self, at_parent):
        _seed_pipeline(
            at_parent,
            "collide",
            steps=_array(_step("first"), _step("second", id="step_0")),
        )

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        assert "collides" in str(excinfo.value)

    def test_every_offending_pipeline_is_named_not_just_the_first(self, at_parent):
        """One upgrade attempt tells the operator everything that needs a
        human, rather than making them discover it one restart at a time."""
        _seed_pipeline(at_parent, "bad-1", name="First Bad", steps="{nope")
        _seed_pipeline(at_parent, "bad-2", name="Second Bad", steps="[[[")
        _seed_pipeline(at_parent, "good", steps=_array(_step("a")))

        with pytest.raises(RuntimeError) as excinfo:
            _upgrade(at_parent, REVISION)

        message = str(excinfo.value)
        assert "bad-1" in message and "bad-2" in message
        assert "2 pipeline definition(s)" in message

    def test_a_refusal_converts_no_row_and_does_not_advance_the_version(
        self, at_parent
    ):
        """The good row in the same batch must not be left converted while
        the migration is recorded as never having run - that is a database
        whose state matches no revision."""
        _seed_pipeline(at_parent, "good", steps=_array(_step("a", id="a")))
        _seed_pipeline(at_parent, "bad", steps="{nope")

        with pytest.raises(RuntimeError):
            _upgrade(at_parent, REVISION)

        assert _row(at_parent, "good")["steps_graph"] is None
        with at_parent.connect() as conn:
            version = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
        assert version == PARENT

    def test_the_added_column_survives_a_refusal_and_the_retry_tolerates_it(
        self, at_parent
    ):
        """MEASURED, not assumed, because it is the opposite of what the
        surrounding `engine.begin()` implies.

        pysqlite only issues a BEGIN before DML, so `ALTER TABLE ... ADD
        COLUMN` runs in autocommit and SURVIVES the rollback that discards
        every UPDATE. A refusal therefore leaves the database stamped at
        the parent revision with `definition_error` already present - which
        is harmless only because the revision is guarded and re-runnable.
        This test is what keeps that true: remove the `if 'definition_error'
        not in columns` guard and the operator's second attempt dies with
        'duplicate column name' instead of naming the pipeline they have to
        fix.
        """
        _seed_pipeline(at_parent, "bad", steps="{nope")

        with pytest.raises(RuntimeError):
            _upgrade(at_parent, REVISION)
        assert "definition_error" in _columns(at_parent, "pipelines")

        # The operator fixes the definition and runs the upgrade again.
        with at_parent.begin() as conn:
            conn.execute(
                sa.text("UPDATE pipelines SET steps = :s WHERE id = 'bad'"),
                {"s": _array(_step("a", id="a"))},
            )

        _upgrade(at_parent, REVISION)

        assert _graph(at_parent, "bad")["entry_points"] == ["a"]


class TestInFlightRunsAreReportedNotRewritten:
    """A run that started on the array path has StepRuns with step_id NULL.

    Backfilling `step_runs.step_id = 'step_' || step_index` is only correct
    if the pipeline still has the steps it had when the run started - which
    nothing here can know - so the revision reports them and leaves them
    alone. This is the same objection 0007 raised when it refused to relabel
    `executor='legacy'` as `'local'`.
    """

    def test_a_running_runs_step_ids_are_left_null(self, at_parent, caplog):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a", id="a")))
        with at_parent.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO pipeline_runs (id, pipeline_id, status, "
                    "trigger_type, current_step, steps_completed, steps_total, "
                    "created_at) VALUES ('run-1', 'p1', 'running', 'manual', "
                    "0, 0, 1, '2026-08-31')"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO step_runs (id, pipeline_run_id, step_index, "
                    "step_id, step_name, status, logs) VALUES ('sr-1', "
                    "'run-1', 0, NULL, 'a', 'running', '')"
                )
            )

        with caplog.at_level("WARNING", logger="alembic.runtime.migration"):
            _upgrade(at_parent, REVISION)

        with at_parent.connect() as conn:
            step_id = conn.execute(
                sa.text("SELECT step_id FROM step_runs WHERE id = 'sr-1'")
            ).scalar()
        assert step_id is None, (
            "inventing a step_id here would be the migration inventing "
            "history"
        )
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING"
        ]
        assert any("pending/running" in message for message in warnings), (
            "an in-flight run that will fail after the cutover must be "
            f"reported, not discovered later. Warnings seen: {warnings}"
        )
        assert any("1 pipeline run(s)" in message for message in warnings), (
            "the warning must carry the COUNT, so the operator knows how "
            "much history this cutover touched"
        )


# -----------------------------------------------------------------------------
# Downgrade
# -----------------------------------------------------------------------------

class TestDowngrade:
    """The schema change is fully reversible. The data fill deliberately is
    not, and the revision's docstring says so rather than implying
    otherwise."""

    def test_the_column_goes_and_the_table_survives(self, at_parent):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a", id="a")))
        _upgrade(at_parent, REVISION)

        _downgrade(at_parent, PARENT)

        assert "definition_error" not in _columns(at_parent, "pipelines")
        assert _row(at_parent, "p1")["name"] == "A Pipeline"

    def test_an_inbound_run_still_joins_after_the_rebuild(self, at_parent):
        """`op.batch_alter_table` REBUILDS the table on SQLite. `pipelines`
        is the target of pipeline_runs.pipeline_id, so a rebuild that lost
        rows or ids would orphan every run in the database."""
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a", id="a")))
        _upgrade(at_parent, REVISION)
        with at_parent.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO pipeline_runs (id, pipeline_id, status, "
                    "trigger_type, current_step, steps_completed, steps_total, "
                    "created_at) VALUES ('run-1', 'p1', 'passed', 'manual', "
                    "0, 1, 1, '2026-08-31')"
                )
            )

        _downgrade(at_parent, PARENT)

        with at_parent.connect() as conn:
            joined = conn.execute(
                sa.text(
                    "SELECT p.name FROM pipeline_runs r JOIN pipelines p "
                    "ON p.id = r.pipeline_id WHERE r.id = 'run-1'"
                )
            ).scalar()
        assert joined == "A Pipeline"

    def test_the_backfilled_graph_survives_and_so_does_the_original_array(
        self, at_parent
    ):
        array = _array(_step("a", id="a"))
        _seed_pipeline(at_parent, "p1", steps=array)
        _upgrade(at_parent, REVISION)
        backfilled = _row(at_parent, "p1")["steps_graph"]

        _downgrade(at_parent, PARENT)

        row = _row(at_parent, "p1")
        assert row["steps"] == array, (
            "the array is never modified, so the pre-0014 shape is fully "
            "readable after a downgrade"
        )
        assert row["steps_graph"] == backfilled, (
            "un-filling would mean NULLing author-written graphs too - "
            "steps_graph does not record who wrote it"
        )

    def test_upgrade_downgrade_upgrade_round_trips(self, at_parent):
        _seed_pipeline(at_parent, "p1", steps=_array(_step("a", id="a")))

        _upgrade(at_parent, REVISION)
        first = _row(at_parent, "p1")["steps_graph"]
        _downgrade(at_parent, PARENT)
        _upgrade(at_parent, REVISION)

        row = _row(at_parent, "p1")
        assert row["steps_graph"] == first
        assert row["definition_error"] is None
        assert "definition_error" in _columns(at_parent, "pipelines")


# -----------------------------------------------------------------------------
# The frozen converter
# -----------------------------------------------------------------------------

def _revision_module(tmp_path):
    engine = _engine(tmp_path, "chain.db")
    try:
        with engine.connect() as conn:
            script = ScriptDirectory.from_config(_alembic_config(conn))
        return script.get_revision(REVISION).module
    finally:
        engine.dispose()


class TestTheConverterIsFrozen:
    """s4.7: the converter in the revision is a deliberate copy and must
    stay one.

    `alembic/env.py` puts `app` on the path, so importing the live
    `array_to_graph` would WORK. The argument against it is not that it
    fails - it is that it rots. A migration must produce the same output for
    the same old row no matter which commit the operator runs it from, and
    `array_to_graph` is live code that keeps changing to serve the two
    authoring edges.
    """

    def test_the_revision_imports_no_application_code(self, tmp_path):
        source = Path(_revision_module(tmp_path).__file__).read_text(
            encoding="utf-8"
        )
        offending = [
            line
            for line in source.splitlines()
            if line.startswith(("import app", "from app"))
            or line.strip().startswith(("import app.", "from app."))
        ]
        assert not offending, (
            "0014 imports live application code: "
            f"{offending}. A migration that calls `array_to_graph` produces "
            "a different graph for the same old row depending on which "
            "commit it is run from. The chain's precedent is unanimous - "
            "zero `from app` imports across all thirteen prior revisions"
        )

    @pytest.mark.parametrize(
        "steps",
        [
            pytest.param([{"name": "a", "type": "script"}], id="single"),
            pytest.param(
                [
                    {"name": "a", "type": "script", "on_success": "next",
                     "on_failure": "stop"},
                    {"name": "b", "type": "script", "on_success": "next",
                     "on_failure": "stop"},
                    {"name": "c", "type": "agent", "on_success": "next",
                     "on_failure": "stop"},
                ],
                id="linear-chain",
            ),
            pytest.param(
                [{"id": "build", "name": "B", "type": "script"},
                 {"id": "test", "name": "T", "type": "script"}],
                id="authored-ids",
            ),
            pytest.param(
                [{"name": "a", "type": "script", "on_success": "merge:main"}],
                id="merge-on-the-last-step",
            ),
            pytest.param(
                [{"name": "a", "type": "script", "on_success": "merge:release"},
                 {"name": "b", "type": "script"}],
                id="merge-mid-array",
            ),
            pytest.param(
                [{"name": "a", "type": "agent", "on_failure": "trigger:card-1"},
                 {"name": "b", "type": "script"}],
                id="trigger-on-failure",
            ),
            pytest.param(
                [{"name": "a", "type": "script", "on_success": "next",
                  "on_failure": "next"},
                 {"name": "b", "type": "docker", "config": {"image": "x"},
                  "timeout": 900, "continue_in_context": True}],
                id="both-conditions-and-settings",
            ),
        ],
    )
    def test_the_frozen_copy_agreed_with_the_live_converter_when_written(
        self, tmp_path, steps
    ):
        """A dated fidelity check, NOT a contract to be maintained.

        This is the only thing that can tell a faithful frozen copy from a
        hand-typed approximation with a bug in it, so it is worth having on
        the day the revision is authored.

        IF THIS EVER GOES RED, THE CORRECT RESPONSE IS TO DELETE THIS TEST -
        recording in its place that the live converter has moved on - and
        NEVER to edit 0014 to match. The revision is a historical record of
        how rows were converted on 2026-08-31; changing it changes what
        already-migrated databases claim to have done. Every other test in
        this file exercises 0014 on its own terms and survives that deletion.
        """
        from app.schemas.pipeline import PipelineStepConfig, array_to_graph

        frozen = _revision_module(tmp_path)._array_to_graph(
            json.loads(json.dumps(steps))
        )
        live = array_to_graph(
            [PipelineStepConfig.model_validate(s) for s in steps]
        ).model_dump(mode="json")

        assert frozen == live

    def test_the_frozen_copy_refuses_where_the_live_one_refuses(self, tmp_path):
        from app.schemas.pipeline import (
            ArrayConversionError,
            PipelineStepConfig,
            array_to_graph,
        )

        module = _revision_module(tmp_path)
        refused = [
            [],
            [{"name": "a", "type": "script", "on_success": "trigger:pipeline:x"}],
            [{"name": "a", "type": "script", "on_success": "banana"}],
            [{"name": "a", "type": "script", "on_success": "stop",
              "on_failure": "stop"},
             {"name": "b", "type": "script"}],
            [{"id": "x", "name": "a", "type": "script"},
             {"id": "x", "name": "b", "type": "script"}],
        ]
        for steps in refused:
            with pytest.raises(module._ArrayConversionError):
                module._array_to_graph(json.loads(json.dumps(steps)))
            with pytest.raises(ArrayConversionError):
                array_to_graph([PipelineStepConfig.model_validate(s) for s in steps])

    @pytest.mark.parametrize(
        "steps",
        [
            pytest.param(["not an object"], id="a-step-that-is-a-string"),
            pytest.param([{"type": "script"}], id="no-name"),
            pytest.param([{"name": "a", "type": "banana"}], id="unknown-type"),
        ],
    )
    def test_the_frozen_copy_refuses_what_pydantic_refused_for_the_live_one(
        self, tmp_path, steps
    ):
        """The live converter never sees these: its caller builds
        `PipelineStepConfig` first, so a malformed step is a ValidationError
        before `array_to_graph` is entered. The frozen copy reads raw dicts
        and has no such gatekeeper, so it has to say it itself - and it must
        REFUSE rather than write a node the application would then fail to
        parse back out of the column.
        """
        import pydantic

        from app.schemas.pipeline import PipelineStepConfig

        with pytest.raises(pydantic.ValidationError):
            [PipelineStepConfig.model_validate(s) for s in steps]

        module = _revision_module(tmp_path)
        with pytest.raises(module._ArrayConversionError):
            module._array_to_graph(json.loads(json.dumps(steps)))


# -----------------------------------------------------------------------------
# The adopt path
# -----------------------------------------------------------------------------

class TestAnAdoptedDatabaseIsBackfilledToo:
    """`_adopt_unversioned` is the path a pre-alembic dev database takes, and
    it is the one place the backfill could be skipped without anybody
    noticing (s4.8's shape).

    It classifies by asking what is MISSING. A database built before this
    wave has a `pipelines` table with no `definition_error` - and
    `create_all` never adds columns to an EXISTING table - so it is not
    head-shaped, gets stamped at the baseline, and the caller's
    upgrade-to-head runs this revision properly. That is the reasoning; this
    test is what makes it a fact rather than an argument.
    """

    def test_an_unversioned_database_with_array_rows_lands_at_head_converted(
        self, tmp_path
    ):
        from app.database import _run_migrations

        engine = _engine(tmp_path, "adopted.db")
        try:
            # The BASELINE shape, which is what `create_all` plus the old
            # hand-rolled ALTER hacks built before alembic existed - not a
            # migrated database with its version table removed. This is the
            # shape the `lazyaf-data` docker volume actually holds.
            _upgrade(engine, "0001")
            _seed_repo(engine)
            _seed_pipeline(
                engine,
                "legacy",
                steps=_array(
                    _step("build", id="build"), _step("test", id="test")
                ),
            )
            # What a pre-alembic database looks like: app tables, no version.
            with engine.begin() as conn:
                conn.execute(sa.text("DROP TABLE alembic_version"))

            with engine.begin() as conn:
                _run_migrations(conn)

            with engine.connect() as conn:
                version = conn.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar()
                heads = ScriptDirectory.from_config(
                    _alembic_config(conn)
                ).get_heads()
            assert version in heads

            assert "definition_error" in _columns(engine, "pipelines")
            graph = _graph(engine, "legacy")
            assert list(graph["steps"]) == ["build", "test"]
            assert graph["entry_points"] == ["build"]
        finally:
            engine.dispose()
