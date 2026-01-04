"""enhance_runners_table

Revision ID: a1b2c3d4e5f6
Revises: 9495b26aec48
Create Date: 2026-01-03 18:00:00.000000

Phase 12.6: Enhance runners table for WebSocket-based remote execution.

Adds:
- name: Human-readable runner name
- runner_type: claude-code, gemini, generic
- labels: JSON dict for hardware requirements matching
- current_step_execution_id: FK to step_executions (replaces current_job_id)
- websocket_id: Unique ID for WebSocket connection tracking
- connected_at: When runner connected via WebSocket
- created_at: When runner record was created

Updates status enum to include new states:
- disconnected, connecting, idle, assigned, busy, dead
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '9495b26aec48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add new columns to runners table
    with op.batch_alter_table('runners', schema=None) as batch_op:
        # Add name column
        batch_op.add_column(
            sa.Column('name', sa.String(length=255), nullable=True)
        )

        # Add runner_type column with default
        batch_op.add_column(
            sa.Column('runner_type', sa.String(length=50), nullable=False,
                     server_default='claude-code')
        )

        # Add labels column (JSON stored as text for SQLite compatibility)
        batch_op.add_column(
            sa.Column('labels', sa.Text(), nullable=True)
        )

        # Add current_step_execution_id (FK to step_executions)
        batch_op.add_column(
            sa.Column('current_step_execution_id', sa.String(length=36), nullable=True)
        )

        # Add websocket_id for connection tracking
        batch_op.add_column(
            sa.Column('websocket_id', sa.String(length=64), nullable=True)
        )

        # Add connected_at timestamp
        batch_op.add_column(
            sa.Column('connected_at', sa.DateTime(), nullable=True)
        )

        # Add created_at timestamp
        batch_op.add_column(
            sa.Column('created_at', sa.DateTime(), nullable=True)
        )

        # Create foreign key for current_step_execution_id
        batch_op.create_foreign_key(
            'fk_runners_step_execution',
            'step_executions',
            ['current_step_execution_id'],
            ['id']
        )

        # Create index on websocket_id for connection lookup
        batch_op.create_index(
            'ix_runners_websocket_id',
            ['websocket_id'],
            unique=True
        )

        # Create index on status for querying idle runners
        batch_op.create_index(
            'ix_runners_status',
            ['status'],
            unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('runners', schema=None) as batch_op:
        # Drop indexes
        batch_op.drop_index('ix_runners_status')
        batch_op.drop_index('ix_runners_websocket_id')

        # Drop foreign key
        batch_op.drop_constraint('fk_runners_step_execution', type_='foreignkey')

        # Drop columns
        batch_op.drop_column('created_at')
        batch_op.drop_column('connected_at')
        batch_op.drop_column('websocket_id')
        batch_op.drop_column('current_step_execution_id')
        batch_op.drop_column('labels')
        batch_op.drop_column('runner_type')
        batch_op.drop_column('name')
