"""Model endpoints: self-hosted OpenAI-compatible servers become first class (M14.1).

Adds:
- model_endpoints: one row per addressable (server, model) pair, with the
  capability record a probe writes and the cost coordinates a step carries
- step_executions.model_endpoint_id + the admission gate's index

THREE SCOPE DECISIONS, restated here because a migration is where they become
irreversible:

- **Nothing is added to `step_usages`.** The endpoint join goes through the
  existing `step_usages.gpu_node_id`, which is why `model_endpoints.gpu_node_id`
  is NOT NULL and defaults to `endpoint:<name>`. A materialized
  `model_endpoint_id` on the usage table would be a second writer for a fact
  the join already carries - the same discipline 12.6.5 applied when it
  refused to copy `cost_usd` onto `experiment_runs`. It also keeps historical
  usage priceable after an endpoint is deleted.

- **`step_executions.model_endpoint_id` carries NO database-level FOREIGN
  KEY.** SQLite cannot ALTER a constraint into place, so one would require a
  batch REBUILD of `step_executions` - a table that `runners
  .current_step_execution_id` and `step_usages.step_execution_id` both point
  at, and whose rebuild under SQLite's rename semantics is a real risk for
  those references. The column follows the precedent already set by
  `step_usages.pipeline_run_id` and `test_runs.experiment_run_id`, and
  `DELETE /api/model-endpoints/{id}` nulls referencing rows EXPLICITLY - which
  is what actually happens on SQLite in any case, since this app never enables
  `PRAGMA foreign_keys`.

- **No column stores a secret.** `auth_secret_ref` is the NAME of a backend
  environment variable, prefix-allowlisted to `LAZYAF_ENDPOINT_*` in the API
  layer. There is no column here that could hold a key, which is the point:
  LazyAF has no secret-at-rest story and a stored key would be a new class of
  exposure introduced for the convenience of one form field.

Guard note (same as 0002/0004/0005/0006/0009/0010): a pre-alembic database
adopted at startup is healed by Base.metadata.create_all before it is stamped
and upgraded, so the objects this revision creates can already exist when it
runs - the table is created only if absent, every index only if missing, and
the column only if not already present. The migration-parity integration test
(tdd/integration/test_migrations.py) pins the resulting schema either way,
index names included.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
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

    # --- model_endpoints -----------------------------------------------------
    if not inspector.has_table('model_endpoints'):
        op.create_table(
            'model_endpoints',
            sa.Column('id', sa.String(length=36), nullable=False),
            # The handle every other surface uses (model: "endpoint:<name>").
            # Capped at 40 so `endpoint:<name>` fits gpu_node_id's String(64).
            sa.Column('name', sa.String(length=40), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            # The OpenAI-compatible ROOT including the version segment.
            sa.Column('base_url', sa.String(length=512), nullable=False),
            sa.Column('model', sa.String(length=200), nullable=False),
            # Forensics and probe HINTS only - never behavior.
            sa.Column(
                'server_kind', sa.String(length=24), nullable=False,
                server_default='other',
            ),
            sa.Column(
                'auth_style', sa.String(length=16), nullable=False,
                server_default='none',
            ),
            # The NAME of a backend env var. NEVER a value.
            sa.Column('auth_secret_ref', sa.String(length=64), nullable=True),
            sa.Column('auth_header_name', sa.String(length=64), nullable=True),
            sa.Column(
                'reach', sa.String(length=16), nullable=False,
                server_default='direct',
            ),
            sa.Column('runner_label', sa.String(length=64), nullable=True),
            # Money is NUMERIC, never float (models/usage.py's rule). NULL is
            # unpriced; 0.000000 is a different, meaningful value.
            sa.Column('rate_usd_hour', sa.Numeric(precision=18, scale=6), nullable=True),
            # NOT NULL: this is the join key into step_usages.gpu_node_id.
            sa.Column('gpu_node_id', sa.String(length=64), nullable=False),
            sa.Column(
                'max_concurrency', sa.Integer(), nullable=False, server_default='1'
            ),
            sa.Column(
                'request_timeout_seconds', sa.Integer(), nullable=False,
                server_default='300',
            ),
            # Operator OVERRIDE only; what the probe discovered lives in
            # probe_detail, so an override survives every re-probe.
            sa.Column('context_window', sa.Integer(), nullable=True),
            sa.Column('max_output_tokens', sa.Integer(), nullable=True),
            # THREE-STATE. NULL = never probed = dispatch refuses.
            sa.Column('supports_tools', sa.Boolean(), nullable=True),
            sa.Column('supports_streaming', sa.Boolean(), nullable=True),
            sa.Column('reports_usage', sa.Boolean(), nullable=True),
            sa.Column(
                'probe_status', sa.String(length=16), nullable=False,
                server_default='unprobed',
            ),
            sa.Column(
                'probe_detail', sa.Text(), nullable=False, server_default='{}'
            ),
            sa.Column('probed_at', sa.DateTime(), nullable=True),
            sa.Column('probed_from', sa.String(length=64), nullable=True),
            sa.Column('probe_harness_version', sa.String(length=64), nullable=True),
            sa.Column(
                'consecutive_failures', sa.Integer(), nullable=False,
                server_default='0',
            ),
            sa.Column('last_success_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column(
                'enabled', sa.Boolean(), nullable=False, server_default=sa.text('1')
            ),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
    inspector = sa.inspect(op.get_bind())
    _ensure_index(
        inspector, 'model_endpoints', 'ix_model_endpoints_name', ['name'], True,
    )
    _ensure_index(
        inspector, 'model_endpoints', 'ix_model_endpoints_gpu_node_id',
        ['gpu_node_id'], False,
    )
    _ensure_index(
        inspector, 'model_endpoints', 'ix_model_endpoints_enabled_reach',
        ['enabled', 'reach'], False,
    )

    # --- step_executions: the admission gate's column and index --------------
    step_execution_columns = {
        col['name'] for col in inspector.get_columns('step_executions')
    }
    if 'model_endpoint_id' not in step_execution_columns:
        # No FK - see the module docstring. Plain add_column, no rebuild.
        op.add_column(
            'step_executions',
            sa.Column('model_endpoint_id', sa.String(length=36), nullable=True),
        )
    inspector = sa.inspect(op.get_bind())
    _ensure_index(
        inspector, 'step_executions', 'ix_step_executions_endpoint_status',
        ['model_endpoint_id', 'status'], False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Reverse order: the step_executions index and column (the slot bookkeeping
    is meaningless without the table that gave it meaning), then the endpoint
    indexes and the table.
    """
    op.drop_index(
        'ix_step_executions_endpoint_status', table_name='step_executions'
    )
    op.drop_column('step_executions', 'model_endpoint_id')

    op.drop_index('ix_model_endpoints_enabled_reach', table_name='model_endpoints')
    op.drop_index('ix_model_endpoints_gpu_node_id', table_name='model_endpoints')
    op.drop_index('ix_model_endpoints_name', table_name='model_endpoints')
    op.drop_table('model_endpoints')
