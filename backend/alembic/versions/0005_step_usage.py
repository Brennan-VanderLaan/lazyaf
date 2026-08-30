"""Usage channel: step_usages (Phase 12.5).

Adds the control-layer protocol's fourth channel (status / logs /
test-results / usage) to the schema: one accounting row per StepExecution,
written by POST /api/steps/{id}/usage (app/models/usage.py).

Two scope decisions carried from the design and worth restating here,
because a migration is where they become irreversible:

- `role` IS in this revision, NULL everywhere in 12.5. `cost_by_role` is
  unrecoverable after the fact, so the column ships with the frozen wire
  rather than after it.
- `trial_iteration_id` is deliberately NOT in this revision. Nothing writes
  it and there is no table to reference; it lands with M13's trials table.

Guard note (same as 0002/0004): a pre-alembic database adopted at startup is
healed by Base.metadata.create_all before it is stamped and upgraded, so the
objects this revision creates can already exist when it runs — the table is
created only if absent and each index only if missing. The migration-parity
integration test (tdd/integration/test_migrations.py) pins the resulting
schema either way.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_indexes() -> None:
    # Idempotency key: a retrying runtime UPDATES, never double-bills.
    op.create_index(
        'ix_step_usages_step_execution_id',
        'step_usages',
        ['step_execution_id'],
        unique=True,
    )
    # The run rollup is read-heavy and groups by role (api-surface s6).
    op.create_index(
        'ix_step_usages_pipeline_run_id_role',
        'step_usages',
        ['pipeline_run_id', 'role'],
        unique=False,
    )


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table('step_usages'):
        op.create_table('step_usages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('step_execution_id', sa.String(length=36), nullable=False),
        sa.Column('step_run_id', sa.String(length=36), nullable=True),
        # DENORMALIZED and deliberately not an FK: the rollup must not join to
        # reach the run, and accounting rows outlive run pruning.
        sa.Column('pipeline_run_id', sa.String(length=36), nullable=True),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('model_version', sa.String(length=128), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_write_tokens', sa.Integer(), nullable=True),
        # Money is NUMERIC, never float. SQLite stores it as REAL; the
        # ingestion service quantizes to 6dp on write so it round-trips exact.
        sa.Column('cost_usd', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('cost_source', sa.String(length=16), nullable=False),
        sa.Column('wall_clock_ms', sa.Integer(), nullable=False),
        sa.Column('container_seconds', sa.Float(), nullable=True),
        sa.Column('gpu_node_id', sa.String(length=64), nullable=True),
        sa.Column('gpu_fraction', sa.Float(), nullable=True),
        # M13 attribution: on the wire and in the schema now, NULL until the
        # strategy fan-out fills it.
        sa.Column('role', sa.String(length=64), nullable=True),
        sa.Column('determinism', sa.Text(), nullable=False),
        sa.Column('raw', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['step_execution_id'], ['step_executions.id'], ),
        sa.ForeignKeyConstraint(['step_run_id'], ['step_runs.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        _create_indexes()
    else:
        # Pre-existing table: only reachable via the adopt path, where
        # _adopt_unversioned (app/database.py) already healed the schema with
        # create_all and owns drift detection — skip creation, but backfill
        # any index that table was built without.
        existing_indexes = {ix['name'] for ix in inspector.get_indexes('step_usages')}
        if 'ix_step_usages_step_execution_id' not in existing_indexes:
            op.create_index(
                'ix_step_usages_step_execution_id',
                'step_usages',
                ['step_execution_id'],
                unique=True,
            )
        if 'ix_step_usages_pipeline_run_id_role' not in existing_indexes:
            op.create_index(
                'ix_step_usages_pipeline_run_id_role',
                'step_usages',
                ['pipeline_run_id', 'role'],
                unique=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_step_usages_pipeline_run_id_role', table_name='step_usages')
    op.drop_index('ix_step_usages_step_execution_id', table_name='step_usages')
    op.drop_table('step_usages')
