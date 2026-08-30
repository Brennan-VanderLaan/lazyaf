"""Runner registry: the runners table becomes real (Phase 12.6).

Before this revision `runners` was a dead five-column table (id,
container_id, status, current_job_id, last_heartbeat) that
`routers/runners.py` imported and never queried, while `RunnerPool` kept
every fact in process memory. 12.6 makes the row the durable projection of a
live WebSocket connection, so a restart, a dispatcher scan, or the UI can
read it.

ADD ONLY. Nothing is dropped here: `container_id` and `current_job_id` are
polling-stack leftovers and they go in 0007, in the deletion commit, once
nothing reads them. A migration that both adds the new world and removes the
old one cannot be reverted in halves.

Two things beyond columns, and both are load-bearing:

- **Data migration.** `status='offline'` (the old RunnerStatus vocabulary)
  becomes `'disconnected'` (the RunnerState vocabulary, cross-agent contract
  #4). failure_01's version of this table left that gap open, so rows
  carried a status no enum recognized. On top of that, EVERY row is forced to
  `disconnected` with `websocket_id = NULL` and `current_step_execution_id
  = NULL`: no connection survives a migration, and pretending one did is how
  a fresh backend hands work to a ghost.
- **`step_executions.runner_requirements` / `assigned_at`.** A requeued step
  must be re-matchable by the dispatcher AFTER a backend restart, so the
  `requires:` block has to be durable rather than held in the dispatch
  closure. `assigned_at` is ACK-timeout forensics.

Guard note (same as 0002/0004/0005): a pre-alembic database adopted at
startup is healed by Base.metadata.create_all before it is stamped and
upgraded, so every object this revision creates can already exist when it
runs - each add is guarded by a column/index-name check. The migration-parity
integration test (tdd/integration/test_migrations.py) pins the resulting
schema either way.

Batch mode note: `current_step_execution_id` carries a FOREIGN KEY, and
SQLite cannot ALTER a constraint into place - alembic's SQLiteImpl raises
NotImplementedError on a plain add_column that carries one. The runners
changes therefore run inside `op.batch_alter_table` (copy-and-move), which
is also why the table is rebuilt rather than appended to.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every column 0006 adds to `runners`, in creation order.
_RUNNER_COLUMNS = (
    ('name', lambda: sa.Column('name', sa.String(length=255), nullable=True)),
    (
        'runner_type',
        lambda: sa.Column(
            'runner_type',
            sa.String(length=50),
            nullable=False,
            server_default='claude-code',
        ),
    ),
    ('labels', lambda: sa.Column('labels', sa.Text(), nullable=True)),
    (
        'current_step_execution_id',
        lambda: sa.Column(
            'current_step_execution_id', sa.String(length=36), nullable=True
        ),
    ),
    ('websocket_id', lambda: sa.Column('websocket_id', sa.String(length=64), nullable=True)),
    ('protocol_version', lambda: sa.Column('protocol_version', sa.Integer(), nullable=True)),
    ('agent_version', lambda: sa.Column('agent_version', sa.String(length=64), nullable=True)),
    ('connected_at', lambda: sa.Column('connected_at', sa.DateTime(), nullable=True)),
    ('created_at', lambda: sa.Column('created_at', sa.DateTime(), nullable=True)),
)


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    runner_columns = {col['name'] for col in inspector.get_columns('runners')}
    runner_indexes = {ix['name'] for ix in inspector.get_indexes('runners')}
    runner_fks = {fk.get('name') for fk in inspector.get_foreign_keys('runners')}

    missing = [factory() for name, factory in _RUNNER_COLUMNS if name not in runner_columns]
    needs_fk = 'fk_runners_step_execution' not in runner_fks
    missing_indexes = [
        name
        for name in ('ix_runners_websocket_id', 'ix_runners_status')
        if name not in runner_indexes
    ]

    if missing or needs_fk or missing_indexes:
        with op.batch_alter_table('runners') as batch_op:
            for column in missing:
                batch_op.add_column(column)
            if needs_fk:
                batch_op.create_foreign_key(
                    'fk_runners_step_execution',
                    'step_executions',
                    ['current_step_execution_id'],
                    ['id'],
                )
            if 'ix_runners_websocket_id' in missing_indexes:
                batch_op.create_index(
                    'ix_runners_websocket_id', ['websocket_id'], unique=True
                )
            if 'ix_runners_status' in missing_indexes:
                batch_op.create_index('ix_runners_status', ['status'], unique=False)

    # --- data migration ------------------------------------------------------
    # The old RunnerStatus vocabulary is gone; 'offline' (and any blank left
    # by a half-written row) becomes RunnerState.DISCONNECTED.
    op.execute(
        "UPDATE runners SET status = 'disconnected' "
        "WHERE status IN ('offline', '') OR status IS NULL"
    )
    # No connection survives a migration. Every row lands disconnected with
    # no socket and no claimed step, so the registry's bootstrap and the
    # orphan sweep start from the truth instead of from a stale claim.
    op.execute(
        "UPDATE runners SET status = 'disconnected', websocket_id = NULL, "
        "current_step_execution_id = NULL"
    )

    # --- step_executions -----------------------------------------------------
    step_execution_columns = {
        col['name'] for col in inspector.get_columns('step_executions')
    }
    if 'runner_requirements' not in step_execution_columns:
        op.add_column(
            'step_executions', sa.Column('runner_requirements', sa.Text(), nullable=True)
        )
    if 'assigned_at' not in step_execution_columns:
        op.add_column(
            'step_executions', sa.Column('assigned_at', sa.DateTime(), nullable=True)
        )

    step_execution_indexes = {
        ix['name'] for ix in inspector.get_indexes('step_executions')
    }
    if 'ix_step_executions_status' not in step_execution_indexes:
        # The dispatcher scans status == 'pending' on every wake.
        op.create_index(
            'ix_step_executions_status', 'step_executions', ['status'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_step_executions_status', table_name='step_executions')
    op.drop_column('step_executions', 'assigned_at')
    op.drop_column('step_executions', 'runner_requirements')

    with op.batch_alter_table('runners') as batch_op:
        batch_op.drop_index('ix_runners_status')
        batch_op.drop_index('ix_runners_websocket_id')
        batch_op.drop_constraint('fk_runners_step_execution', type_='foreignkey')
        for name, _factory in reversed(_RUNNER_COLUMNS):
            batch_op.drop_column(name)
