"""add_debug_sessions_table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-01-04 10:00:00.000000

Phase 12.7: Add debug_sessions table for debug re-run mode.

Debug sessions allow users to:
- Re-run failed pipelines with breakpoints
- Connect via CLI for interactive debugging
- Resume or abort execution at breakpoints
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create debug_sessions table."""
    op.create_table('debug_sessions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('pipeline_run_id', sa.String(length=36), nullable=False),
        sa.Column('original_run_id', sa.String(length=36), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('breakpoints', sa.Text(), nullable=False),
        sa.Column('current_step_index', sa.Integer(), nullable=True),
        sa.Column('current_step_name', sa.String(length=255), nullable=True),
        sa.Column('connection_mode', sa.String(length=20), nullable=True),
        sa.Column('sidecar_container_id', sa.String(length=64), nullable=True),
        sa.Column('token', sa.String(length=128), nullable=False),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False, server_default='3600'),
        sa.Column('max_timeout_seconds', sa.Integer(), nullable=False, server_default='14400'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('breakpoint_hit_at', sa.DateTime(), nullable=True),
        sa.Column('connected_at', sa.DateTime(), nullable=True),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('state_history', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['pipeline_run_id'], ['pipeline_runs.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('debug_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_debug_sessions_pipeline_run_id'), ['pipeline_run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_debug_sessions_status'), ['status'], unique=False)


def downgrade() -> None:
    """Drop debug_sessions table."""
    with op.batch_alter_table('debug_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_debug_sessions_status'))
        batch_op.drop_index(batch_op.f('ix_debug_sessions_pipeline_run_id'))

    op.drop_table('debug_sessions')
