"""Workspaces table and step_runs.executor column (Phase 12.2-INT).

Adds:
- workspaces: one named-Docker-volume workspace per pipeline run
  (app/models/workspace.py). No FK into the pipeline tables by design —
  consumers query by pipeline_run_id.
- step_runs.executor: which execution path ran the step
  ("local" | "legacy" | "remote"), set at dispatch time.

Guard note: a pre-alembic database adopted at startup is healed by
Base.metadata.create_all (which builds the CURRENT model schema) before it
is stamped and upgraded — see _adopt_unversioned in app/database.py. The
objects this revision creates can therefore already exist when it runs, so
each is created only if absent. The migration-parity integration test
(tdd/integration/test_migrations.py) pins the resulting schema either way.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table('workspaces'):
        op.create_table('workspaces',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('pipeline_run_id', sa.String(length=36), nullable=False),
        sa.Column('repo_id', sa.String(length=36), nullable=False),
        sa.Column('volume_name', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('use_count', sa.Integer(), nullable=False),
        sa.Column('branch', sa.String(length=255), nullable=True),
        sa.Column('commit_sha', sa.String(length=40), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('cleaned_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('volume_name')
        )
        op.create_index(op.f('ix_workspaces_pipeline_run_id'), 'workspaces', ['pipeline_run_id'], unique=True)
        op.create_index(op.f('ix_workspaces_repo_id'), 'workspaces', ['repo_id'], unique=False)
        op.create_index(op.f('ix_workspaces_status'), 'workspaces', ['status'], unique=False)
    else:
        # Pre-existing table: only reachable via the adopt path, where
        # _adopt_unversioned (app/database.py) already healed the schema with
        # create_all and owns drift detection — skip creation, but backfill
        # the status index for tables built before it existed.
        existing_indexes = {ix['name'] for ix in inspector.get_indexes('workspaces')}
        if 'ix_workspaces_status' not in existing_indexes:
            op.create_index(op.f('ix_workspaces_status'), 'workspaces', ['status'], unique=False)

    step_run_columns = {col['name'] for col in inspector.get_columns('step_runs')}
    if 'executor' not in step_run_columns:
        op.add_column('step_runs', sa.Column('executor', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('step_runs', 'executor')
    op.drop_index(op.f('ix_workspaces_status'), table_name='workspaces')
    op.drop_index(op.f('ix_workspaces_repo_id'), table_name='workspaces')
    op.drop_index(op.f('ix_workspaces_pipeline_run_id'), table_name='workspaces')
    op.drop_table('workspaces')
