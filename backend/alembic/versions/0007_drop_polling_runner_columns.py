"""Drop the polling stack's leftovers (Phase 12.6 deletion commit).

0006 was deliberately ADD-ONLY: it built the registry's world beside the old
one so a half-applied migration could not leave the table meaning neither
thing. This is the other half, and it ships in the DELETION COMMIT rather
than with 0006 - by the time it runs, `runner_pool`, `job_queue`, the three
polling entrypoints and the whole `/api/runners` polling surface are gone, so
nothing can read what it removes.

Two columns and one data migration:

- `runners.container_id` / `runners.current_job_id`. Both were written by the
  polling pool, which addressed a runner by the container it happened to live
  in and the job it happened to hold. A 12.6 runner is addressed by its
  socket (`websocket_id`) and holds a STEP EXECUTION
  (`current_step_execution_id`, added in 0006 and written on every
  assignment - its absence was the single omission that neutered the salvaged
  attempt's entire recovery service).

- `step_runs.executor = 'legacy'` -> NULL. `ExecutorMode` no longer has a
  LEGACY member, and `StepRunRead.executor` is typed as that enum precisely so
  an off-vocabulary value is a loud validation error rather than a silently
  misread string (cross-file contract #3). Left alone, every historical run
  that used the polling path would 500 the run-detail endpoint.

  NULL, not 'local'. Rewriting those rows to 'local' would be the gate lying
  about history - the step did not run on the local executor, and R1's whole
  point is that the executor field is a record of what actually happened.
  NULL reads as "run by a path this system no longer has", which is exactly
  true. The count is logged so the operator sees how much history this
  touched rather than discovering it later.

Downgrade re-adds both columns as nullable. It cannot restore their values -
they were process-local facts about containers that no longer exist - and it
cannot restore 'legacy' either, because the rows no longer record which of
them had it. A downgrade past this point gives you the old SHAPE, not the old
data, and saying so here is better than implying otherwise.

Guard note (same as 0002/0004/0005/0006): a pre-alembic database adopted at
startup is healed by Base.metadata.create_all before it is stamped, so the
columns this revision drops may already be absent. Every step is guarded by a
column-name check and the revision is re-runnable.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-30
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

#: Columns the polling pool owned. Nothing reads them after the deletion.
_DEAD_RUNNER_COLUMNS = ('container_id', 'current_job_id')


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- runners: drop the polling columns -----------------------------------
    runner_columns = {col['name'] for col in inspector.get_columns('runners')}
    doomed = [name for name in _DEAD_RUNNER_COLUMNS if name in runner_columns]
    if doomed:
        # batch mode: SQLite cannot DROP COLUMN in place on older versions, and
        # the table already carries a foreign key from 0006 that a naive
        # rebuild would lose.
        with op.batch_alter_table('runners') as batch_op:
            for name in doomed:
                batch_op.drop_column(name)

    # --- step_runs: retire the 'legacy' executor value -----------------------
    step_run_columns = {col['name'] for col in inspector.get_columns('step_runs')}
    if 'executor' in step_run_columns:
        result = bind.execute(
            sa.text(
                "UPDATE step_runs SET executor = NULL WHERE executor = 'legacy'"
            )
        )
        if result.rowcount:
            logger.info(
                "0007: cleared executor='legacy' on %s historical step run(s) - "
                "the polling path they record no longer exists, and NULL says "
                "that rather than relabelling them 'local'",
                result.rowcount,
            )


def downgrade() -> None:
    """Downgrade schema.

    Re-adds the column SHAPE only. The values were process-local facts about
    containers and queued jobs that are gone, and the 'legacy' executor rows
    no longer record which ones they were.
    """
    inspector = sa.inspect(op.get_bind())
    runner_columns = {col['name'] for col in inspector.get_columns('runners')}

    missing = [name for name in _DEAD_RUNNER_COLUMNS if name not in runner_columns]
    if missing:
        with op.batch_alter_table('runners') as batch_op:
            if 'container_id' in missing:
                batch_op.add_column(
                    sa.Column('container_id', sa.String(length=64), nullable=True)
                )
            if 'current_job_id' in missing:
                batch_op.add_column(
                    sa.Column('current_job_id', sa.String(length=36), nullable=True)
                )
