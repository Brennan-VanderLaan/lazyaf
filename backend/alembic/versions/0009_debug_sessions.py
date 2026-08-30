"""Debug sessions: the debug re-run's one durable row (Phase 12.7).

One table, `debug_sessions`. It is the SINGLE source of truth for debug state
(R3): no `RunStatus` member was added for it - `RunStatus` has five members
pinned by dozens of tests and every UI colour map - and no in-memory registry
mirrors it. The paused breakpoint gate re-reads this row on every wake.

Three column choices worth the ink, because each replaces something
failure_01's version of this table got wrong:

- **`pipeline_run_id` is UNIQUE**, not merely indexed. The executor gate
  looks a session up by run id on every step; two sessions for one run would
  make "which one pauses this step?" unanswerable, and the database is the
  right place to make that unrepresentable.

- **There is no `token` column.** failure_01 stored a long-lived secret here
  and returned it from a GET the UI polls. The join credential is now a
  15-minute re-mintable JWT (`POST /api/debug/{id}/join-token`), so there is
  no stored secret to leak - and revocation is free, because the terminal
  upgrade re-reads this row and refuses a terminal session whatever the JWT
  says.

- **`hit_breakpoints` is durable.** A key is appended when its gate fires, so
  a re-dispatched step cannot re-pause on a breakpoint already serviced, and
  "which breakpoints never fired" is answerable at session end. failure_01
  kept that bookkeeping in process memory, where a restart lost it.

`state_history` is WIRED here, unlike the identically-named column on
failure_01's table: every transition goes through `DebugStateMachine` and the
serialized history lands in this column.

Migration parent: `0007_drop_polling_runner_columns`. 12.6.6 was pre-assigned
`0008` and released it back to the pool (see `upcoming/wave6-1266-wiring.md`
section 8), so `0007` is the real head. If a `0008` appears before this lands,
THIS `down_revision` is the line to change - and the integrator, not the
implementer, confirms it.

Guard note (same as 0002/0004/0005/0006/0007): a pre-alembic database adopted
at startup is healed by `Base.metadata.create_all` before it is stamped, so
this table can already exist when the revision runs - hence the inspector
guard rather than a bare `create_table`.

Revision ID: 0009
Revises: 0007
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table('debug_sessions'):
        op.create_table(
            'debug_sessions',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('pipeline_run_id', sa.String(length=36), nullable=False),
            sa.Column('original_run_id', sa.String(length=36), nullable=True),
            sa.Column('status', sa.String(length=32), nullable=False),
            sa.Column('breakpoints', sa.Text(), nullable=False),
            sa.Column('hit_breakpoints', sa.Text(), nullable=False),
            sa.Column('current_step_key', sa.String(length=255), nullable=True),
            sa.Column('current_step_name', sa.String(length=255), nullable=True),
            sa.Column('current_step_index', sa.Integer(), nullable=True),
            sa.Column('current_step_executor', sa.String(length=16), nullable=True),
            sa.Column('sidecar_container_id', sa.String(length=64), nullable=True),
            sa.Column('connection_mode', sa.String(length=16), nullable=True),
            sa.Column('timeout_seconds', sa.Integer(), nullable=False),
            sa.Column('max_timeout_seconds', sa.Integer(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('breakpoint_hit_at', sa.DateTime(), nullable=True),
            sa.Column('connected_at', sa.DateTime(), nullable=True),
            sa.Column('ended_at', sa.DateTime(), nullable=True),
            sa.Column('end_reason', sa.String(length=255), nullable=True),
            sa.Column('state_history', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['pipeline_run_id'], ['pipeline_runs.id'], ),
            sa.PrimaryKeyConstraint('id'),
        )
        # Exactly the three access paths, and no more:
        #   - the gate's per-step lookup, by run id (UNIQUE: one session per run)
        #   - the "what is paused right now" scan, by status
        #   - "which debug re-runs came from this failed run", by original_run_id
        op.create_index(
            op.f('ix_debug_sessions_pipeline_run_id'),
            'debug_sessions',
            ['pipeline_run_id'],
            unique=True,
        )
        op.create_index(
            op.f('ix_debug_sessions_status'), 'debug_sessions', ['status'], unique=False
        )
        op.create_index(
            op.f('ix_debug_sessions_original_run_id'),
            'debug_sessions',
            ['original_run_id'],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the table outright. Nothing else in the schema references it - a
    debug session points AT a pipeline run, never the other way round - so
    there is no orphaned column to heal and no data migration to reverse. A
    downgrade past this point loses debug history, which is exactly what
    dropping the only table that holds it means.
    """
    inspector = sa.inspect(op.get_bind())

    if inspector.has_table('debug_sessions'):
        op.drop_index(op.f('ix_debug_sessions_original_run_id'), table_name='debug_sessions')
        op.drop_index(op.f('ix_debug_sessions_status'), table_name='debug_sessions')
        op.drop_index(op.f('ix_debug_sessions_pipeline_run_id'), table_name='debug_sessions')
        op.drop_table('debug_sessions')
