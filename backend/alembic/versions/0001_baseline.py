"""Baseline: full schema as of Milestone 12 attempt #3 Phase 0.

Captures every table previously created by Base.metadata.create_all plus the
hand-rolled ALTER hacks that used to live in database.py (steps_graph,
active_step_ids/completed_step_ids, step_runs.step_id, step_executions extras).
Pre-alembic databases are stamped at this revision instead of running it.

Revision ID: 0001
Revises:
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('agent_files',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('repos',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=True),
    sa.Column('remote_url', sa.String(length=1024), nullable=True),
    sa.Column('default_branch', sa.String(length=255), nullable=False),
    sa.Column('is_ingested', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('runners',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('container_id', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('current_job_id', sa.String(length=36), nullable=True),
    sa.Column('last_heartbeat', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('pipelines',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('repo_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('steps', sa.Text(), nullable=False),
    sa.Column('steps_graph', sa.Text(), nullable=True),
    sa.Column('triggers', sa.Text(), nullable=False),
    sa.Column('is_template', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['repo_id'], ['repos.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('pipeline_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('pipeline_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('trigger_type', sa.String(length=50), nullable=False),
    sa.Column('trigger_ref', sa.String(length=255), nullable=True),
    sa.Column('trigger_context', sa.Text(), nullable=True),
    sa.Column('current_step', sa.Integer(), nullable=False),
    sa.Column('steps_completed', sa.Integer(), nullable=False),
    sa.Column('steps_total', sa.Integer(), nullable=False),
    sa.Column('active_step_ids', sa.Text(), nullable=True),
    sa.Column('completed_step_ids', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['pipeline_id'], ['pipelines.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cards',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('repo_id', sa.String(length=36), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('runner_type', sa.String(length=50), nullable=False),
    sa.Column('step_type', sa.String(length=50), nullable=False),
    sa.Column('step_config', sa.Text(), nullable=True),
    sa.Column('prompt_template', sa.Text(), nullable=True),
    sa.Column('agent_file_ids', sa.Text(), nullable=True),
    sa.Column('branch_name', sa.String(length=255), nullable=True),
    sa.Column('pr_url', sa.String(length=1024), nullable=True),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('completed_runner_type', sa.String(length=50), nullable=True),
    sa.Column('pipeline_run_id', sa.String(length=36), nullable=True),
    sa.Column('pipeline_step_index', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['pipeline_run_id'], ['pipeline_runs.id'], ),
    sa.ForeignKeyConstraint(['repo_id'], ['repos.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('step_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('pipeline_run_id', sa.String(length=36), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=False),
    sa.Column('step_id', sa.String(length=64), nullable=True),
    sa.Column('step_name', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('logs', sa.Text(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['pipeline_run_id'], ['pipeline_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('card_id', sa.String(length=36), nullable=False),
    sa.Column('runner_id', sa.String(length=36), nullable=True),
    sa.Column('runner_type', sa.String(length=50), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('logs', sa.Text(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('step_type', sa.String(length=50), nullable=False),
    sa.Column('step_config', sa.Text(), nullable=True),
    sa.Column('tests_run', sa.Boolean(), nullable=False),
    sa.Column('tests_passed', sa.Boolean(), nullable=True),
    sa.Column('test_pass_count', sa.Integer(), nullable=True),
    sa.Column('test_fail_count', sa.Integer(), nullable=True),
    sa.Column('test_skip_count', sa.Integer(), nullable=True),
    sa.Column('test_output', sa.Text(), nullable=True),
    sa.Column('step_run_id', sa.String(length=36), nullable=True),
    sa.ForeignKeyConstraint(['card_id'], ['cards.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('step_executions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('execution_key', sa.String(length=255), nullable=False),
    sa.Column('step_run_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('runner_id', sa.String(length=36), nullable=True),
    sa.Column('container_id', sa.String(length=64), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('progress', sa.Text(), nullable=True),
    sa.Column('last_heartbeat', sa.DateTime(), nullable=True),
    sa.Column('timeout_at', sa.DateTime(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['step_run_id'], ['step_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_step_executions_execution_key'), 'step_executions', ['execution_key'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_step_executions_execution_key'), table_name='step_executions')
    op.drop_table('step_executions')
    op.drop_table('jobs')
    op.drop_table('step_runs')
    op.drop_table('cards')
    op.drop_table('pipeline_runs')
    op.drop_table('pipelines')
    op.drop_table('runners')
    op.drop_table('repos')
    op.drop_table('agent_files')
