"""Experiments: matrix, cells and frozen prompt bodies (Phase 12.6.5).

Adds:
- experiments: one question, expressed as a matrix, with a HARD budget cap
- experiment_runs: one matrix cell = one ad-hoc agent run
- prompt_versions: the immutable body a cell actually ran
- test_runs.experiment_run_id / .prompt_version + the aggregation index

MIGRATION NUMBER — STATED, NOT SILENT. This lane was pre-assigned 0007 and
its design (upcoming/wave6-1265-wiring.md s0.1) moved to 0008, because Phase
12.6's deletion commit had already spent 0007. Both are now wrong, for a
reason the design could not have known: Phase 12.7 landed
`0009_debug_sessions.py` FIRST, parented on 0007, on the stated assumption
that no 0008 would appear.

Taking 0008 off 0007 at this point FORKS the chain: alembic then reports
"Multiple head revisions are present" and `init_db` cannot bring ANY database
to head - the app stops booting, not merely the parity test. The two ways out
were (a) edit 12.7's `down_revision` to '0008', which is another lane's file,
or (b) linearize on top of what actually landed. This revision takes (b):
`0010`, `down_revision='0009'`, chain `0007 -> 0009 -> 0010`, single head, no
cross-lane edit, and 12.7's stated assumption stays true.

INTEGRATOR: if you prefer the design's ordering, it is four lines - rename
this file to 0008_experiments.py, set `revision='0008'` and
`down_revision='0007'` here, set `down_revision='0008'` in
0009_debug_sessions.py (whose own docstring asks for exactly that edit), and
set ALEMBIC_HEAD_REVISION accordingly in tdd/integration/test_migrations.py.
Nothing else in this phase references the number.

Three scope decisions carried from the design, restated here because a
migration is where they become irreversible:

- NO `cost_usd` / `tests_passed` columns on experiment_runs. `step_usages`
  and `test_runs` are the only sources of truth for money and outcomes; a
  materialized copy would be a second writer, and a second writer drifts.
  The join key (`step_usages.pipeline_run_id`) and its index already exist
  from 0005, so cost-per-cell costs no new schema.

- NO `pipeline_runs.experiment_id`. `trigger_type='experiment'` +
  `trigger_ref=<cell id>` are already-persisted columns that say the same
  thing, and they are written at run CREATION — which matters because
  `start_pipeline` can complete a run synchronously, before any column set
  afterwards could land.

- `test_runs.experiment_run_id` is deliberately NOT a foreign key, for the
  same reason `test_runs.pipeline_run_id` is not: runs are provenance
  records that must survive pruning. It carries no FK, so plain
  `op.add_column` suffices and the SQLite table rebuild that 0007 needed is
  not required here.

Guard note (same as 0002/0004/0005/0006/0007/0009): a pre-alembic database
adopted at startup is healed by Base.metadata.create_all before it is
stamped and upgraded, so the objects this revision creates can already
exist when it runs — every table is created only if absent, every index only
if missing, and every column only if not already present. The
migration-parity integration test (tdd/integration/test_migrations.py) pins
the resulting schema either way, index names included.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ensure_index(inspector, table: str, name: str, columns: list, unique: bool) -> None:
    """Create an index only when the table lacks it (adopt-path safe)."""
    existing = {ix['name'] for ix in inspector.get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    # --- experiments ---------------------------------------------------------
    if not inspector.has_table('experiments'):
        op.create_table(
            'experiments',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('description', sa.Text(), nullable=False),
            sa.Column('target_type', sa.String(length=32), nullable=False),
            # NOT an FK: an experiment's provenance must survive its target
            # being deleted.
            sa.Column('target_id', sa.String(length=36), nullable=False),
            sa.Column('repo_id', sa.String(length=36), nullable=False),
            sa.Column('matrix', sa.Text(), nullable=False),
            sa.Column('verify', sa.Text(), nullable=True),
            # Money is NUMERIC, never float (models/usage.py's rule).
            sa.Column('budget_usd', sa.Numeric(precision=18, scale=6), nullable=False),
            sa.Column('max_concurrency', sa.Integer(), nullable=False),
            sa.Column('cell_timeout', sa.Integer(), nullable=False),
            sa.Column('push_branches', sa.Boolean(), nullable=False),
            sa.Column('status', sa.String(length=24), nullable=False),
            sa.Column('estimated_cost_usd', sa.Numeric(precision=18, scale=6), nullable=True),
            sa.Column('estimate_basis', sa.String(length=24), nullable=True),
            # The cap bounds DISPATCH; spend in flight when it trips is
            # recorded here rather than quietly absorbed.
            sa.Column('budget_overrun_usd', sa.Numeric(precision=18, scale=6), nullable=False),
            sa.Column('created_by', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('launched_at', sa.DateTime(), nullable=True),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['repo_id'], ['repos.id'], ),
            sa.PrimaryKeyConstraint('id'),
        )
    inspector = sa.inspect(op.get_bind())
    _ensure_index(
        inspector, 'experiments', 'ix_experiments_status_created_at',
        ['status', 'created_at'], False,
    )
    _ensure_index(
        inspector, 'experiments', 'ix_experiments_target_type_target_id',
        ['target_type', 'target_id'], False,
    )

    # --- prompt_versions (before experiment_runs: it is referenced by it) ----
    if not inspector.has_table('prompt_versions'):
        op.create_table(
            'prompt_versions',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('template_id', sa.String(length=36), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            # The FROZEN text that actually ran. Never updated: PromptTemplate
            # .content is the editable draft, this is the record of what ran.
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('content_hash', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ['template_id'], ['prompt_templates.id'], ondelete='CASCADE'
            ),
            sa.PrimaryKeyConstraint('id'),
        )
    inspector = sa.inspect(op.get_bind())
    _ensure_index(
        inspector, 'prompt_versions', 'ix_prompt_versions_template_id_version',
        ['template_id', 'version'], True,
    )
    _ensure_index(
        inspector, 'prompt_versions', 'ix_prompt_versions_template_id_content_hash',
        ['template_id', 'content_hash'], True,
    )

    # --- experiment_runs -----------------------------------------------------
    if not inspector.has_table('experiment_runs'):
        op.create_table(
            'experiment_runs',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('experiment_id', sa.String(length=36), nullable=False),
            sa.Column('cell_index', sa.Integer(), nullable=False),
            sa.Column('variant_index', sa.Integer(), nullable=False),
            sa.Column('agent', sa.String(length=32), nullable=False),
            sa.Column('model', sa.String(length=128), nullable=True),
            sa.Column('prompt_template_id', sa.String(length=36), nullable=True),
            sa.Column('prompt_version_id', sa.String(length=36), nullable=True),
            sa.Column('prompt_version', sa.Integer(), nullable=True),
            sa.Column('label', sa.String(length=128), nullable=True),
            sa.Column('repeat_index', sa.Integer(), nullable=False),
            # Convenience mirror only. The LINK is PipelineRun.trigger_ref.
            sa.Column('pipeline_run_id', sa.String(length=36), nullable=True),
            sa.Column('status', sa.String(length=24), nullable=False),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ['experiment_id'], ['experiments.id'], ondelete='CASCADE'
            ),
            sa.ForeignKeyConstraint(['prompt_template_id'], ['prompt_templates.id'], ),
            sa.ForeignKeyConstraint(['prompt_version_id'], ['prompt_versions.id'], ),
            sa.PrimaryKeyConstraint('id'),
        )
    inspector = sa.inspect(op.get_bind())
    _ensure_index(
        inspector, 'experiment_runs',
        'ix_experiment_runs_experiment_id_cell_index',
        ['experiment_id', 'cell_index'], True,
    )
    _ensure_index(
        inspector, 'experiment_runs', 'ix_experiment_runs_experiment_id_status',
        ['experiment_id', 'status'], False,
    )
    _ensure_index(
        inspector, 'experiment_runs', 'ix_experiment_runs_pipeline_run_id',
        ['pipeline_run_id'], False,
    )

    # --- test_runs: the experiment coordinates -------------------------------
    test_run_columns = {col['name'] for col in inspector.get_columns('test_runs')}
    if 'experiment_run_id' not in test_run_columns:
        op.add_column(
            'test_runs',
            sa.Column('experiment_run_id', sa.String(length=36), nullable=True),
        )
    if 'prompt_version' not in test_run_columns:
        op.add_column(
            'test_runs', sa.Column('prompt_version', sa.Integer(), nullable=True)
        )
    inspector = sa.inspect(op.get_bind())
    _ensure_index(
        inspector, 'test_runs', 'ix_test_runs_experiment_run_id_test_ref_id',
        ['experiment_run_id', 'test_ref_id'], False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops in reverse: the test_runs index and columns (the experiment
    coordinates are lost, because the tables that gave them meaning are going
    too), then experiment_runs, prompt_versions and experiments.
    """
    op.drop_index(
        'ix_test_runs_experiment_run_id_test_ref_id', table_name='test_runs'
    )
    op.drop_column('test_runs', 'prompt_version')
    op.drop_column('test_runs', 'experiment_run_id')

    op.drop_index('ix_experiment_runs_pipeline_run_id', table_name='experiment_runs')
    op.drop_index(
        'ix_experiment_runs_experiment_id_status', table_name='experiment_runs'
    )
    op.drop_index(
        'ix_experiment_runs_experiment_id_cell_index', table_name='experiment_runs'
    )
    op.drop_table('experiment_runs')

    op.drop_index(
        'ix_prompt_versions_template_id_content_hash', table_name='prompt_versions'
    )
    op.drop_index('ix_prompt_versions_template_id_version', table_name='prompt_versions')
    op.drop_table('prompt_versions')

    op.drop_index('ix_experiments_target_type_target_id', table_name='experiments')
    op.drop_index('ix_experiments_status_created_at', table_name='experiments')
    op.drop_table('experiments')
