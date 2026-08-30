"""Test tie-back: test_refs + test_runs (Phase 12.2.6).

Adds:
- test_refs: registrations of declared lazyaf_test_ids, per repo, optionally
  joined to acceptance_criteria (app/models/testref.py)
- test_runs: observed executions joined to TestRef + pipeline run + commit;
  model/prompt_template_id reserved for Phase 12.6.5 experiment context

Guard note (same as 0003): a pre-alembic database adopted at startup is
healed by Base.metadata.create_all before it is stamped and upgraded, so the
objects this revision creates can already exist when it runs — each is
created only if absent. The migration-parity integration test
(tdd/integration/test_migrations.py) pins the resulting schema either way.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table('test_refs'):
        op.create_table('test_refs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('lazyaf_test_id', sa.String(length=255), nullable=False),
        sa.Column('repo_id', sa.String(length=36), nullable=False),
        sa.Column('file_path', sa.String(length=1024), nullable=True),
        sa.Column('criterion_id', sa.String(length=36), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['criterion_id'], ['acceptance_criteria.id'], ),
        sa.ForeignKeyConstraint(['repo_id'], ['repos.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_test_refs_criterion_id'), 'test_refs', ['criterion_id'], unique=False)
        # Identity is (repo_id, lazyaf_test_id): the same marker string in two
        # repos is two independent refs. Leading repo_id also serves every
        # repo-scoped lookup, so there is no separate repo_id index.
        op.create_index(
            'ix_test_refs_repo_id_lazyaf_test_id',
            'test_refs',
            ['repo_id', 'lazyaf_test_id'],
            unique=True,
        )

    if not inspector.has_table('test_runs'):
        op.create_table('test_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('test_ref_id', sa.String(length=36), nullable=False),
        sa.Column('pipeline_run_id', sa.String(length=36), nullable=False),
        sa.Column('step_run_id', sa.String(length=36), nullable=True),
        sa.Column('commit_sha', sa.String(length=64), nullable=False),
        sa.Column('branch', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('model', sa.String(length=255), nullable=True),
        sa.Column('prompt_template_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['prompt_template_id'], ['prompt_templates.id'], ),
        sa.ForeignKeyConstraint(['test_ref_id'], ['test_refs.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        # Exactly the two access paths: criterion history / the blocks-done
        # freshness check walk (test_ref_id, created_at); ingestion's
        # idempotency lookup is by step_run_id. Nothing queries runs by
        # pipeline_run_id or by created_at alone, so neither is indexed.
        op.create_index(
            'ix_test_runs_test_ref_id_created_at',
            'test_runs',
            ['test_ref_id', 'created_at'],
            unique=False,
        )
        op.create_index('ix_test_runs_step_run_id', 'test_runs', ['step_run_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_test_runs_step_run_id', table_name='test_runs')
    op.drop_index('ix_test_runs_test_ref_id_created_at', table_name='test_runs')
    op.drop_table('test_runs')
    op.drop_index('ix_test_refs_repo_id_lazyaf_test_id', table_name='test_refs')
    op.drop_index(op.f('ix_test_refs_criterion_id'), table_name='test_refs')
    op.drop_table('test_refs')
