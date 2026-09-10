"""Drop `pipelines.steps`: the v1 array format leaves the database (12.8 P6).

This is the LAST revision of the v1 retirement and the only one that removes
anything. It lands after the acceptance gate, which is what R2 ("delete only
after acceptance") means here: 0014 backfilled every array-only row into a
graph, the executor's array fork was deleted at P5 with every reader ported,
the dogfood pipeline ran green through the graph executor on backfilled data,
and only then does the column go.

WHY THE SPLIT WAS WORTH IT
--------------------------
Recon recommended one revision doing backfill AND drop, on the grounds that
`steps` is `nullable=False` with NO server_default (0001 declares a bare
`sa.Column('steps', sa.Text(), nullable=False)`), so any window where the
model has stopped declaring the field while the column still exists is a
backend that cannot INSERT a pipeline. That hazard is real, but it only bites
if the model field is removed before the column - and it is not: this
revision and the removal of `Pipeline.steps` from
`backend/app/models/pipeline.py` ship in the same commit. Keeping them
together is what let the migration split in two, and the split is what put an
acceptance gate BETWEEN the irreversible-in-practice data fill and the
irreversible-in-fact column drop.

THE REBUILD, AND WHY THIS TABLE TOLERATES IT
--------------------------------------------
SQLite has no `ALTER TABLE ... DROP COLUMN` before 3.35 and alembic's
`batch_alter_table` therefore RENAMES the table, recreates it, copies the
rows and drops the original. 0011 refused a rebuild of `step_executions` for
exactly this reason - it is the target of inbound foreign keys whose
resolution across the rename could not be relied on there. `pipelines` is
different, and it was verified rather than assumed: lane 4 ran this rebuild
against a scratch SQLite 3.40.1 database carrying the real three-table shape
(`repos` / `pipelines` / `pipeline_runs`) with rows in all three, under both
`PRAGMA legacy_alter_table=0` and `=1`. In every case the rename succeeded,
the inbound FK `pipeline_runs.pipeline_id -> pipelines.id` still resolved,
and every row survived. `pipelines` also carries zero indexes, so there is
nothing to reconstruct. `tdd/integration/test_migrations_pipeline_retirement.py`
pins the FK-still-joins property so the next reader does not have to
re-derive any of this.

DOWNGRADE RESTORES THE SHAPE, NOT THE DATA
------------------------------------------
Said plainly, in 0007's register. `steps` comes back as
`nullable=False, server_default='[]'`, and every row gets `'[]'`. The arrays
themselves are gone; nothing in the database records what they were.

The `nullable=False` is mandatory, not stylistic:
`test_downgrade_to_baseline_matches_pure_0001_schema` and
`test_roundtrip_restores_head_schema` compare a full schema snapshot against
a database built by a pure `0001`, which declares `steps` NOT NULL. Those two
tests are the real regression gate for this revision and they are not
modified by this wave.

The `server_default='[]'` is a DELIBERATE DIVERGENCE from 0001's bare column,
and it is invisible to those tests because `_schema_snapshot` records
`(type, nullable, primary_key)` and not server defaults. It is what makes the
re-added column addable to a populated table in one statement, with no second
rebuild to strip the default afterwards. A downgraded database also keeps
working: the pre-12.8 application supplied `'[]'` from the model's python-side
default, and the server default now supplies it for anything that does not.

**Rejected**: stashing the source array inside the graph JSON as `source_v1`
to make this lossless. `PipelineGraphModel` declares no such field, so it
would not survive a round trip through the application, and it would create a
second source of truth for the step list (R3).

WHAT DOES NOT HAPPEN HERE
-------------------------
`definition_error` (0014) stays. `StepRun.step_index` stays - it is not an
array concept; it keys the execution key, `LAZYAF_STEP_INDEX`, the websocket
step frames and the state machine, and the graph executor derives it from the
node's position in `steps`. No `pipeline_runs` or `step_runs` row is touched.

Guard note (same as 0002/0004/0005/0006/0007/0009/0010/0011/0012/0014): a
pre-alembic database adopted at startup is healed by
`Base.metadata.create_all`, which builds the CURRENT model schema - and after
this revision the current model has no `steps` at all, so the column may
already be absent when this runs. Both directions are guarded by a
column-presence check and the revision is re-runnable.

**The adoption hole this revision opens, and where it is closed.** A
pre-alembic database that ALREADY HAS `steps` (the `lazyaf-data` docker
volume) is invisible to `_adopt_unversioned`'s classifier, which asks only
what is MISSING. After this revision such a database has everything the
models declare, so it would be stamped at head with `steps` surviving as an
orphan - still NOT NULL, still with no server default - and the next pipeline
INSERT would die with `NOT NULL constraint failed: pipelines.steps` from a
database stamped as current. `backend/app/database.py::_RETIRED_COLUMNS` is
the fix: it names ("pipelines", "steps") -> "0014" so such a database is
stamped at the revision BEFORE this one and this revision then runs properly.
If this revision is ever renumbered, that mapping moves with it.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-31

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    columns = {col['name'] for col in sa.inspect(bind).get_columns('pipelines')}

    if 'steps' not in columns:
        # Re-run, or an adopted database whose create_all heal built the
        # post-12.8 model schema. Nothing to drop; say so rather than letting
        # batch_alter_table raise on a schema this revision is meant to
        # tolerate.
        logger.info(
            "0015: `pipelines.steps` is already absent; nothing to drop"
        )
        return

    # A last, cheap look before the array becomes unrecoverable. 0014 is
    # supposed to have left no row with an array and no graph, and it refuses
    # rather than converting anything it cannot hold - but 0014 could have
    # been run, rows inserted by an older backend, and this run started
    # afterwards. Counting is not a substitute for that revision; it is the
    # difference between "your definitions vanished" and being told which
    # ones, while the column is still there to be read.
    stranded = bind.execute(
        sa.text(
            "SELECT id, name FROM pipelines "
            "WHERE (steps_graph IS NULL OR steps_graph = '') "
            "AND steps IS NOT NULL AND TRIM(steps) NOT IN ('', '[]')"
        )
    ).mappings().all()
    if stranded:
        listed = "\n".join(
            f"  - {row['id']} ({row['name']!r})" for row in stranded
        )
        raise RuntimeError(
            f"0015: refusing to drop `pipelines.steps` while "
            f"{len(stranded)} pipeline(s) still hold a v1 array and no "
            f"graph:\n{listed}\n"
            "Dropping now would delete those definitions outright. Run the "
            "0014 backfill first (`alembic upgrade 0014`), which converts "
            "them or names what it cannot convert; a row it refuses needs a "
            "human to fix or delete the pipeline before this revision can "
            "run."
        )

    # `pipelines` carries zero indexes to reconstruct; the inbound FK from
    # `pipeline_runs.pipeline_id` is re-established by the rebuild (verified,
    # see the module docstring).
    with op.batch_alter_table('pipelines') as batch_op:
        batch_op.drop_column('steps')

    logger.info(
        "0015: dropped `pipelines.steps`. The v1 array format is retired: "
        "`steps_graph` is now the only pipeline definition, at the database, "
        "the wire and the executor alike"
    )


def downgrade() -> None:
    """Downgrade schema.

    Restores the SHAPE of `pipelines.steps`, not its contents - see the
    module docstring. Every row comes back holding `'[]'`, which is what the
    pre-12.8 model's python-side default wrote for every pipeline authored as
    a graph anyway.
    """
    bind = op.get_bind()
    columns = {col['name'] for col in sa.inspect(bind).get_columns('pipelines')}

    if 'steps' in columns:
        logger.info("0015: `pipelines.steps` is already present; nothing to add")
        return

    # NOT NULL is mandatory (the two schema-snapshot tests compare against a
    # pure 0001, which declares it NOT NULL). server_default is what lets a
    # NOT NULL column be added to a populated table without a second pass.
    with op.batch_alter_table('pipelines') as batch_op:
        batch_op.add_column(
            sa.Column(
                'steps', sa.Text(), nullable=False, server_default='[]'
            )
        )

    logger.warning(
        "0015 downgrade: `pipelines.steps` is back and every row holds '[]'. "
        "The v1 arrays themselves are NOT restored - nothing in the database "
        "recorded them once the column was dropped. Each pipeline's real "
        "definition is still in `steps_graph`, which this downgrade leaves "
        "untouched"
    )
